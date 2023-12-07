# Copyright 2023-present Daniel Han-Chen & the Unsloth team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
from typing import Optional, Tuple, List, Union
from torch.nn.functional import scaled_dot_product_attention
from transformers.models.llama.modeling_llama import (
    _prepare_4d_causal_attention_mask,
    logger,
    BaseModelOutputWithPast,
    CausalLMOutputWithPast,
)
from ..kernels import *
from ._utils import (
    prepare_model_for_kbit_training,
)

# Get Flash Attention v2 if Ampere (RTX 30xx, A100)
major_version, minor_version = torch.cuda.get_device_capability()
if major_version >= 8:
    try:
        from flash_attn import flash_attn_func
        HAS_FLASH_ATTENTION = True
    except:
        HAS_FLASH_ATTENTION = False
else:
    # Tri Dao's benchmark shows xformers is faster for now.
    HAS_FLASH_ATTENTION = False
pass
import xformers.ops.fmha as xformers
xformers_attention = xformers.memory_efficient_attention

# Final patching code
from transformers.models.llama.modeling_llama import (
    LlamaAttention,
    LlamaDecoderLayer,
    LlamaModel,
    LlamaForCausalLM,
) 
from peft import PeftModelForCausalLM
import gc
import peft
import bitsandbytes as bnb
import numpy as np
import types

from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig, AutoConfig
from transformers import set_seed as transformers_set_seed
from peft import LoraConfig, TaskType, get_peft_model as _get_peft_model


def original_apply_qkv(self, X):
    Q = self.q_proj(X)
    K = self.k_proj(X)
    V = self.v_proj(X)
    return Q, K, V
pass


def original_apply_o(self, X):
    O = self.o_proj(X)
    return O
pass


def LlamaAttention_fast_forward_inference(
    self,
    hidden_states:  torch.Tensor,
    past_key_value: Optional[Tuple[torch.Tensor]],
    position_ids,
):
    """
        https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py#L406
        Fast inference using KV cache.
        QK^T can be computed in 4 chunks

        [Q, q] @ [K, k].T where q, k are the new tokens.
        [QK^T, Qk^T]
        [qK^T, qk^T]

        Since the attention mask wipes Qk^T, we just get
        [QK^T,    0]
        [qK^T, qk^T]

        Since softmax is row-wise, we get
        softmax([QK^T,    0])
        softmax([qK^T, qk^T])

        We then multiply by   [V]
                              [v]
        softmax([QK^T,    0]) [softmax(QK^T)V] *
        softmax([qK^T, qk^T]) [softmax([qK^T, qk^T]) @ [V, v]]

        But notice * [softmax(QK^T)V] is just the last attention.
        We just need to compute the last final row.

        This means we can pass in a row of Q, but we need to
        remember K and V, which are called the KV cache.
    """
    Xn = hidden_states
    bsz, _, _ = hidden_states.size()
    K1, V1 = past_key_value

    Wq = self.q_proj.weight
    Wk = self.k_proj.weight
    Wv = self.v_proj.weight
    Wo = self.o_proj.weight

    n_heads    = self.num_heads
    n_groups   = self.num_key_value_groups
    n_kv_heads = self.num_key_value_heads
    head_dim   = self.head_dim
    assert(n_kv_heads * n_groups == n_heads)

    Qn, Kn, Vn = original_apply_qkv(self, Xn)
    Qn = Qn.view(bsz, 1, n_heads,    head_dim).transpose(1, 2)
    Kn = Kn.view(bsz, 1, n_kv_heads, head_dim).transpose(1, 2)
    Vn = Vn.view(bsz, 1, n_kv_heads, head_dim).transpose(1, 2)

    kv_seq_len = K1.shape[-2] + 1
    cos, sin = self.rotary_emb(Vn, seq_len = kv_seq_len)
    Qn, Kn = inplace_rope_embedding(Qn, Kn, cos, sin, position_ids)
    
    # New KV cache
    Kn = torch.cat([K1, Kn], dim = 2)
    Vn = torch.cat([V1, Vn], dim = 2)

    # Grouped query attention
    # K = repeat_kv(K, n_groups)
    # V = repeat_kv(V, n_groups)
    if n_groups != 1:
        _, _, cached_len, _ = Kn.shape
        Kn = Kn[:, :, None, :, :].expand(bsz, n_kv_heads, n_groups, cached_len, head_dim)
        Vn = Vn[:, :, None, :, :].expand(bsz, n_kv_heads, n_groups, cached_len, head_dim)
        Kn = Kn.reshape(bsz, n_heads, cached_len, head_dim)
        Vn = Vn.reshape(bsz, n_heads, cached_len, head_dim)
    pass

    # Attention
    A = torch.matmul(Qn, Kn.transpose(2, 3))
    A *= 1.0 / (self.head_dim**0.5)
    A = torch.nn.functional.softmax(A, dim = -1, dtype = torch.float32).to(A.dtype)
    A = torch.matmul(A, Vn)
    A = A.transpose(1, 2)
    A = A.reshape(bsz, 1, self.hidden_size)
    A = original_apply_o(self, A)
    return A, (Kn, Vn)
pass


# https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py#L320
def LlamaAttention_fast_forward(
    self,
    hidden_states:        torch.Tensor,
    causal_mask:          Optional[xformers.attn_bias.BlockDiagonalCausalMask] = None,
    attention_mask:       Optional[torch.Tensor] = None,
    position_ids:         Optional[torch.LongTensor] = None,
    past_key_value:       Optional[Tuple[torch.Tensor]] = None,
    output_attentions:    bool = False,
    use_cache:            bool = False,
    padding_mask:         Optional[torch.LongTensor] = None,
    *args, **kwargs,
) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
    
    bsz, q_len, _ = hidden_states.size()
    Q, K, V = self.apply_qkv(self, hidden_states)

    # Check for inference
    if use_cache and past_key_value is not None and q_len == 1:
        A, past_key_value = LlamaAttention_fast_forward_inference(
            self,
            hidden_states,
            past_key_value,
            position_ids,
        )
        return A, None, past_key_value
    pass

    n_heads    = self.num_heads
    n_groups   = self.num_key_value_groups
    n_kv_heads = self.num_key_value_heads
    head_dim   = self.head_dim
    assert(n_kv_heads * n_groups == n_heads)

    Q = Q.view(bsz, q_len, n_heads,    head_dim).transpose(1, 2)
    K = K.view(bsz, q_len, n_kv_heads, head_dim).transpose(1, 2)
    V = V.view(bsz, q_len, n_kv_heads, head_dim).transpose(1, 2)

    kv_seq_len = K.shape[-2]
    if past_key_value is not None:
        kv_seq_len += past_key_value[0].shape[-2]

    if position_ids is None:
        cos = self.rotary_emb.cos_cached
        sin = self.rotary_emb.sin_cached
        Q, K = fast_rope_embedding(Q, K, cos, sin)
    else:
        cos, sin = self.rotary_emb(V, seq_len = kv_seq_len)
        Q, K = inplace_rope_embedding(Q, K, cos, sin, position_ids)
    pass

    if past_key_value is not None:
        # reuse k, v, self_attention
        K = torch.cat([past_key_value[0], K], dim = 2)
        V = torch.cat([past_key_value[1], V], dim = 2)
    past_key_value = (K, V) if use_cache else None

    # Attention module
    # Xformers doesnt support backward pass for GQA (yet)
    # TEMP fix
    if (n_groups == 1) and (not HAS_FLASH_ATTENTION):
        # Xformers memory efficient attention
        # Also has Flash Attention v2 dispatching
        # (batch_size, n_heads, seq_len, head_dim) -> (batch_size, seq_len, n_heads, head_dim)
        Q = Q.transpose(1, 2)
        K = K.transpose(1, 2)
        V = V.transpose(1, 2)

        # Grouped query attention
        if n_groups != 1:
            Q = Q.reshape(bsz, q_len, n_kv_heads, n_groups, head_dim)
            K = K.reshape(bsz, q_len, n_kv_heads,        1, head_dim)
            V = V.reshape(bsz, q_len, n_kv_heads,        1, head_dim)
            K = K .expand(bsz, q_len, n_kv_heads, n_groups, head_dim)
            V = V .expand(bsz, q_len, n_kv_heads, n_groups, head_dim)
        pass

        A = xformers_attention(Q, K, V, attn_bias = causal_mask)
        A = A.view(bsz, q_len, n_heads, head_dim)

    elif HAS_FLASH_ATTENTION:
        # Flash Attention
        # (batch_size, n_heads, seq_len, head_dim) -> (batch_size, seq_len, n_heads, head_dim)
        Q = Q.transpose(1, 2)
        K = K.transpose(1, 2)
        V = V.transpose(1, 2)

        # Flash Attention v2 auto supports grouped query attention
        A = flash_attn_func(Q, K, V, causal = True)
    else:
        # Grouped query attention
        if n_groups != 1:
            K = K[:, :, None, :, :].expand(bsz, n_kv_heads, n_groups, q_len, head_dim)
            V = V[:, :, None, :, :].expand(bsz, n_kv_heads, n_groups, q_len, head_dim)
            K = K.reshape(bsz, n_heads, q_len, head_dim)
            V = V.reshape(bsz, n_heads, q_len, head_dim)
        pass

        # (batch_size, n_heads, seq_len, seq_len)
        scores = (q @ k.transpose(-2, -1)) * (head_dim ** -0.5)
        scores = scores + attention_mask
        scores = torch.nn.functional.softmax(scores, dim=-1)
        # (batch_size, n_heads, seq_len, head_dim)
        A = scores @ V
        
        # Needs (batch_size, n_heads, seq_len, head_dim)
        # is_casual and attention_mask must not be both set!
        #A = scaled_dot_product_attention(Q, K, V, attn_mask = None, is_causal = True)
        # Go back to (batch_size, seq_len, n_heads, head_dim)
        A = A.transpose(1, 2)
    pass
    attn_output = A.reshape(bsz, q_len, self.hidden_size)
    attn_output = self.apply_o(self, attn_output)
    attn_weights = None
    return attn_output, attn_weights, past_key_value
pass


# https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py#L590
def LlamaDecoderLayer_fast_forward(
    self,
    hidden_states:        torch.Tensor,
    causal_mask:          Optional[xformers.attn_bias.BlockDiagonalCausalMask] = None,
    attention_mask:       Optional[torch.Tensor] = None,
    position_ids:         Optional[torch.LongTensor] = None,
    past_key_value:       Optional[Tuple[torch.Tensor]] = None,
    output_attentions:    Optional[bool] = False,
    use_cache:            Optional[bool] = False,
    padding_mask:         Optional[torch.LongTensor] = None,
    *args, **kwargs,
) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
    """
    Args:
        hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
        attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
            `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under
            returned tensors for more detail.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
            (see `past_key_values`).
        past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
    """
    residual = hidden_states

    hidden_states = fast_rms_layernorm(self.input_layernorm, hidden_states)

    # Self Attention
    hidden_states, self_attn_weights, present_key_value = self.self_attn(
        hidden_states=hidden_states,
        causal_mask=causal_mask,
        attention_mask=attention_mask,
        position_ids=position_ids,
        past_key_value=past_key_value,
        output_attentions=output_attentions,
        use_cache=use_cache,
        padding_mask=padding_mask,
    )
    hidden_states = residual + hidden_states

    # Fully Connected
    residual = hidden_states
    hidden_states = fast_rms_layernorm(self.post_attention_layernorm, hidden_states)
    hidden_states = self.mlp(hidden_states)
    hidden_states = residual + hidden_states

    outputs = (hidden_states,)

    if output_attentions:
        outputs += (self_attn_weights,)

    if use_cache:
        outputs += (present_key_value,)

    return outputs
pass


# https://github.com/huggingface/transformers/blob/main/src/transformers/models/llama/modeling_llama.py#L825
def LlamaModel_fast_forward(
    self,
    input_ids:            torch.LongTensor,
    causal_mask:          Optional[xformers.attn_bias.BlockDiagonalCausalMask] = None,
    attention_mask:       Optional[torch.Tensor] = None,
    position_ids:         Optional[torch.LongTensor] = None,
    past_key_values:      Optional[List[torch.FloatTensor]] = None,
    inputs_embeds:        Optional[torch.FloatTensor] = None,
    use_cache:            Optional[bool] = None,
    output_attentions:    Optional[bool] = None,
    output_hidden_states: Optional[bool] = None,
    return_dict:          Optional[bool] = None,
    *args, **kwargs,
) -> Union[Tuple, BaseModelOutputWithPast]:

    output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
    assert(output_attentions is False)
    output_hidden_states = (
        output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
    )
    use_cache = use_cache if use_cache is not None else self.config.use_cache

    return_dict = return_dict if return_dict is not None else self.config.use_return_dict

    # retrieve input_ids and inputs_embeds
    if input_ids is not None and inputs_embeds is not None:
        raise ValueError("You cannot specify both decoder_input_ids and decoder_inputs_embeds at the same time")
    elif input_ids is not None:
        batch_size, seq_length = input_ids.shape
    elif inputs_embeds is not None:
        batch_size, seq_length, _ = inputs_embeds.shape
    else:
        raise ValueError("You have to specify either decoder_input_ids or decoder_inputs_embeds")

    seq_length_with_past = seq_length
    past_key_values_length = 0

    if past_key_values is not None:
        past_key_values_length = past_key_values[0][0].shape[2]
        seq_length_with_past = seq_length_with_past + past_key_values_length

    # We already handle KV cache position_ids ourselves.
    if (past_key_values_length != 0):
        position_ids = torch.arange(
            past_key_values_length, seq_length + past_key_values_length,
            dtype  = torch.int32,
            device = "cuda",
        )
        position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
    elif position_ids is not None:
        position_ids = position_ids.view(-1, seq_length).to(torch.int32)#.long()
    else:
        position_ids = None

    if position_ids is not None:
        if position_ids.shape[0] != batch_size:
            position_ids = position_ids.repeat((batch_size, 1))

    # embed positions
    if inputs_embeds is None:
        inputs_embeds = self.embed_tokens(input_ids)

    # Ignore attention_mask
    if attention_mask is None:
        padding_mask = None
    else:
        if 0 in attention_mask:
            padding_mask = attention_mask
        else:
            padding_mask = None

        attention_mask = _prepare_4d_causal_attention_mask(
            attention_mask, (batch_size, seq_length), inputs_embeds, past_key_values_length,
        )
    pass

    hidden_states = inputs_embeds

    if self.gradient_checkpointing and self.training:
        if use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
            )
            use_cache = False
    pass

    # decoder layers
    all_hidden_states = () if output_hidden_states else None
    all_self_attns = () if output_attentions else None
    next_decoder_cache = () if use_cache else None

    for idx, decoder_layer in enumerate(self.layers):
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        past_key_value = past_key_values[idx] if past_key_values is not None else None

        if self.gradient_checkpointing and self.training:

            def create_custom_forward(module):
                def custom_forward(*inputs):
                    # None for past_key_value
                    return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                return custom_forward

            layer_outputs = torch.utils.checkpoint.checkpoint(
                create_custom_forward(decoder_layer),
                hidden_states,
                causal_mask,
                attention_mask,
                position_ids,
                use_reentrant=True,
                preserve_rng_state=False,
            )
        else:
            layer_outputs = decoder_layer(
                hidden_states,
                causal_mask=causal_mask,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_value,
                output_attentions=output_attentions,
                use_cache=use_cache,
                padding_mask=padding_mask,
            )

        hidden_states = layer_outputs[0]

        if use_cache:
            next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

        if output_attentions:
            all_self_attns += (layer_outputs[1],)
    pass

    hidden_states = fast_rms_layernorm(self.norm, hidden_states)

    # add hidden states from the last decoder layer
    if output_hidden_states:
        all_hidden_states += (hidden_states,)

    next_cache = next_decoder_cache if use_cache else None
    if not return_dict:
        return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
    return BaseModelOutputWithPast(
        last_hidden_state=hidden_states,
        past_key_values=next_cache,
        hidden_states=all_hidden_states,
        attentions=all_self_attns,
    )
pass


def LlamaForCausalLM_fast_forward(
    self,
    input_ids: torch.LongTensor = None,
    causal_mask: Optional[xformers.attn_bias.BlockDiagonalCausalMask] = None,
    attention_mask: Optional[torch.Tensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    past_key_values: Optional[List[torch.FloatTensor]] = None,
    inputs_embeds: Optional[torch.FloatTensor] = None,
    labels: Optional[torch.LongTensor] = None,
    use_cache: Optional[bool] = None,
    output_attentions: Optional[bool] = None,
    output_hidden_states: Optional[bool] = None,
    return_dict: Optional[bool] = None,
    *args, **kwargs,
) -> Union[Tuple, CausalLMOutputWithPast]:

    if causal_mask is None:
        causal_mask = xformers.attn_bias.LowerTriangularMask()

    output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
    output_hidden_states = (
        output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
    )
    return_dict = return_dict if return_dict is not None else self.config.use_return_dict

    # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
    outputs = self.model(
        input_ids=input_ids,
        causal_mask=causal_mask,
        attention_mask=attention_mask,
        position_ids=position_ids,
        past_key_values=past_key_values,
        inputs_embeds=inputs_embeds,
        use_cache=use_cache,
        output_attentions=output_attentions,
        output_hidden_states=output_hidden_states,
        return_dict=return_dict,
    )

    hidden_states = outputs[0]
    logits = self.lm_head(hidden_states)

    loss = None
    if labels is not None:
        shift_logits = logits
        if not hasattr(self, "extra_ignored_labels"):
            # Fixes https://github.com/unslothai/unsloth/issues/10
            self.extra_ignored_labels = torch.full((self.max_seq_length, 1), -100, device = "cuda")
        pass
        
        shift_labels = torch.hstack((labels[..., 1:], self.extra_ignored_labels[:labels.shape[0]]))
        loss = fast_cross_entropy_loss(
            logits = shift_logits,
            labels = shift_labels,
        )
    pass

    if not return_dict:
        output = (logits,) + outputs[1:]
        return (loss,) + output if loss is not None else output

    return CausalLMOutputWithPast(
        loss=loss,
        logits=logits,
        past_key_values=outputs.past_key_values,
        hidden_states=outputs.hidden_states,
        attentions=outputs.attentions,
    )
pass


def PeftModelForCausalLM_fast_forward(
    self,
    input_ids=None,
    causal_mask=None,
    attention_mask=None,
    inputs_embeds=None,
    labels=None,
    output_attentions=None,
    output_hidden_states=None,
    return_dict=None,
    task_ids=None,
    **kwargs,
):
    return self.base_model(
        input_ids=input_ids,
        causal_mask=causal_mask,
        attention_mask=attention_mask,
        inputs_embeds=inputs_embeds,
        labels=labels,
        output_attentions=output_attentions,
        output_hidden_states=output_hidden_states,
        return_dict=return_dict,
        **kwargs,
    )
pass


class FastLlamaModel:

    @staticmethod
    def pre_patch():
        LlamaAttention      .forward = LlamaAttention_fast_forward
        LlamaDecoderLayer   .forward = LlamaDecoderLayer_fast_forward
        LlamaModel          .forward = LlamaModel_fast_forward
        LlamaForCausalLM    .forward = LlamaForCausalLM_fast_forward
        PeftModelForCausalLM.forward = PeftModelForCausalLM_fast_forward
        return
    pass


    @staticmethod
    def from_pretrained(
        model_name = "meta-llama/Llama-2-7b-hf",
        max_seq_length = 4096,
        dtype = None,
        load_in_4bit = True,
        token = None,
        device_map = "sequential",
        rope_scaling = None,
    ):
        gpu_stats = torch.cuda.get_device_properties(0)
        max_memory = round(gpu_stats.total_memory / 1024 / 1024 / 1024, 3)
        SUPPORTS_BFLOAT16 = torch.cuda.is_bf16_supported()

        statistics = \
            "==((====))==  Unsloth: Fast Llama patching release 2023.12\n"\
           f"   \\\   /|    GPU: {gpu_stats.name}. Max memory: {max_memory} GB\n"\
           f"O^O/ \_/ \\    CUDA compute capability = {gpu_stats.major}.{gpu_stats.minor}\n"\
           f"\        /    Pytorch version: {torch.__version__}. CUDA Toolkit = {torch.version.cuda}\n"\
           f' "-____-"     bfloat16 support = {str(SUPPORTS_BFLOAT16).upper()}\n'
        print(statistics)

        FastLlamaModel.pre_patch()

        if dtype is None:
            dtype = torch.float16 if not SUPPORTS_BFLOAT16 else torch.bfloat16
        elif dtype == torch.bfloat16 and not SUPPORTS_BFLOAT16:
            logger.warning_once("Device does not support bfloat16. Will change to float16.")
            dtype = torch.float16

        assert(dtype == torch.float16 or dtype == torch.bfloat16 or dtype == torch.float32)

        # RoPE scaling
        model_max_seq_length = \
            AutoConfig.from_pretrained(model_name, token = token).max_position_embeddings

        if (rope_scaling is None) and (max_seq_length > model_max_seq_length):
            rope_scaling = max_seq_length / model_max_seq_length
            logger.warning_once(
                f"Unsloth: {model_name} can only handle sequence lengths of of most "\
                f"{model_max_seq_length}.\nBut with kaiokendev's RoPE scaling of "\
                f"{round(rope_scaling, 3)}, it can be magically be extended to "\
                f"{max_seq_length}!"
            )
            rope_scaling = {"type": "linear", "factor": rope_scaling,}
        pass

        bnb_config = None
        if load_in_4bit:
            bnb_config = BitsAndBytesConfig(
                load_in_4bit              = True,
                bnb_4bit_use_double_quant = True,
                bnb_4bit_quant_type       = "nf4",
                bnb_4bit_compute_dtype    = dtype,
            )

        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            device_map = device_map,
            torch_dtype = dtype,
            quantization_config = bnb_config,
            token = token,
            rope_scaling = rope_scaling,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            model_max_length = max_seq_length,
            padding_side = "right",
            token = token,
        )

        if not hasattr(tokenizer, "pad_token"):
            # Fixes https://github.com/unslothai/unsloth/issues/5
            if hasattr(tokenizer, "unk_token"):
                tokenizer.add_special_tokens({"pad_token" : tokenizer.unk_token})
                tokenizer.pad_token = tokenizer.unk_token
            else:
                logger.warning_one(
                    f"{model_name} does not have a padding or unknown token!\n"\
                    f"Will use the EOS token of id {tokenizer.eos_token_id} as padding."
                )
                assert(hasattr(tokenizer, "eos_token"))
                tokenizer.add_special_tokens({"pad_token" : tokenizer.eos_token})
                tokenizer.pad_token = tokenizer.eos_token
            config = model.config.update({"pad_token_id" : tokenizer.eos_token_id})
        pass

        model = FastLlamaModel.post_patch(model)

        # Patch up QKV / O and MLP
        for idx, layer in enumerate(model.model.layers):
            layer.self_attn.apply_qkv = original_apply_qkv
            layer.self_attn.apply_o   = original_apply_o
        pass

        model.max_seq_length = max_seq_length
        return model, tokenizer
    pass


    @staticmethod
    def post_patch(model):
        # Patch model
        layers = model.model.layers

        # Torch.compile fails on embedding matrix??
        # Workaround randomnly fixes it for torch versions < 2.2
        model.model.embed_tokens = torch.nn.Embedding.from_pretrained(model.model.embed_tokens.weight)

        # We also do this for the lm_head
        lm_head = torch.nn.Linear(1, 1, bias = None)
        del lm_head.weight
        lm_head.weight = model.lm_head.weight
        lm_head.in_features  = lm_head.weight.shape[1]
        lm_head.out_features = lm_head.weight.shape[0]
        model.lm_head = lm_head

        # Also patch all dtypes - BnB seems to not allocate the correct type?
        # BnB default dtype seems to be float16!
        correct_dtype = lm_head.weight.dtype

        for name, module in model.named_modules():
            if isinstance(module, (bnb.nn.Linear4bit, peft.tuners.lora.Linear4bit)):
                weight = module.weight
                quant_state = weight.quant_state

                if type(quant_state) is list:
                    # BnB seems to have float16 as default!
                    module.weight.quant_state[2] = correct_dtype # Cast to correct dtype
                else:
                    # https://github.com/TimDettmers/bitsandbytes/pull/763/files
                    quant_state.dtype = correct_dtype
                pass
            pass
        pass

        # Clear deleted GPU items
        gc.collect()
        torch.cuda.empty_cache()
        return model
    pass


    @staticmethod
    def get_peft_model(
        model,
        r = 16,
        target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                          "gate_proj", "up_proj", "down_proj"],
        lora_alpha = 16,
        lora_dropout = 0,
        bias = "none",
        layers_to_transform = None,
        use_gradient_checkpointing = True,
        random_state = 3407,
        max_seq_length = 2048,
    ):
        assert(max_seq_length <= model.max_seq_length)

        if lora_dropout != 0:
            raise TypeError("Unsloth: Fast Llama patching only works with dropout = 0.")
        if bias != "none":
            raise TypeError("Unsloth: Fast Llama patching only works with bias = 'none'.")

        transformers_set_seed(random_state)

        accepted_modules = frozenset(("q_proj", "k_proj", "v_proj", "o_proj",
                                      "gate_proj", "up_proj", "down_proj",),)
        for module in target_modules:
            assert(module in accepted_modules)
        pass

        # Get LoRA
        lora_config = LoraConfig(
            r              = r,
            lora_alpha     = lora_alpha,
            target_modules = target_modules,
            lora_dropout   = 0,
            bias           = "none",
            task_type      = TaskType.CAUSAL_LM,
            layers_to_transform = layers_to_transform,
        )

        model = prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing = use_gradient_checkpointing,
            use_reentrant = True,
        )
        model = _get_peft_model(model, lora_config)

        # Do patching
        for idx, layer in enumerate(model.model.model.layers):

            # MLP patching
            if  hasattr(layer.mlp.gate_proj, "lora_A") and \
                hasattr(layer.mlp.  up_proj, "lora_A") and \
                hasattr(layer.mlp.down_proj, "lora_A"):

                # https://stackoverflow.com/questions/50599045/python-replacing-a-function-within-a-class-of-a-module
                layer.mlp.forward = types.MethodType(apply_lora_mlp, layer.mlp)
            pass

            # QKV attention patching
            if  hasattr(layer.self_attn.q_proj, "lora_A") and \
                hasattr(layer.self_attn.k_proj, "lora_A") and \
                hasattr(layer.self_attn.v_proj, "lora_A"):

                layer.self_attn.apply_qkv = apply_lora_qkv
            pass

            # O attention patching
            if hasattr(layer.self_attn.o_proj, "lora_A"):

                layer.self_attn.apply_o = apply_lora_o
            pass
        pass

        # Patch cross entropy loss labels
        # Fixes https://github.com/unslothai/unsloth/issues/10
        extra_ignored_labels = torch.full((max_seq_length, 1), -100, device = "cuda")
        model.model.extra_ignored_labels = extra_ignored_labels
        internal_model = model
        while hasattr(internal_model, "model"):
            internal_model.max_seq_length = max_seq_length
            internal_model = internal_model.model
        pass
        return model
    pass
pass
