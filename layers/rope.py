import torch
from typing import Optional, Tuple

cos_cached = None
sin_cached = None

### huggingface implementation ###

def init_rope_embeddings(dim, max_position_embeddings=4096, base=10000, device=None, scaling_factor=1.0):
    inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.int64).float().to(device) / dim))
    t = torch.arange(max_position_embeddings, device=device, dtype=torch.int64).type_as(inv_freq)
    t = t / scaling_factor
    freqs = torch.outer(t, inv_freq)
    # Different from paper, but it uses a different permutation in order to obtain the same calculation
    emb = torch.cat((freqs, freqs), dim=-1)
    global cos_cached, sin_cached
    cos_cached = emb.cos().to(torch.get_default_dtype())
    sin_cached = emb.sin().to(torch.get_default_dtype())
    return cos_cached,sin_cached


def get_rope_embeddings(x, seq_len=None):
    # x: [bs, num_attention_heads, seq_len, head_size]
    global cos_cached, sin_cached
    return (
        cos_cached[:seq_len].to(dtype=x.dtype),
        sin_cached[:seq_len].to(dtype=x.dtype),
    )

def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, position_ids, unsqueeze_dim=1):
    """Applies Rotary Position Embedding to the query and key tensors.

    Args:
        q (`torch.Tensor`): The query tensor.
        k (`torch.Tensor`): The key tensor.
        cos (`torch.Tensor`): The cosine part of the rotary embedding.
        sin (`torch.Tensor`): The sine part of the rotary embedding.
        position_ids (`torch.Tensor`):
            The position indices of the tokens corresponding to the query and key tensors. For example, this can be
            used to pass offsetted position ids when working with a KV-cache.
        unsqueeze_dim (`int`, *optional*, defaults to 1):
            The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
            sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
            that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
            k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
            cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
            the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
    Returns:
        `tuple(torch.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
    """
    # properly broadcast cos and sin to the dimensions of q and k
    # cos shape torch.Size([seq_len = 13, head_dim = 128])
    # cos[position_ids] shape torch.Size([1, seq_len = 13, head_dim = 128])
    cos = cos[position_ids].unsqueeze(unsqueeze_dim)
    # cos shape torch.Size([1, 1, seq_len = 13, head_dim = 128])
    sin = sin[position_ids].unsqueeze(unsqueeze_dim)
    # q shape torch.Size([batch size = 1, num_head = 32, seq_len = 13, head_dim = 128])
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed

### meta implementation ###

def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0):
    """
    Precompute the frequency tensor for complex exponentials (cis) with given dimensions.

    This function calculates a frequency tensor with complex exponentials using the given dimension 'dim'
    and the end index 'end'. The 'theta' parameter scales the frequencies.
    The returned tensor contains complex values in complex64 data type.

    Args:
        dim (int): Dimension of the frequency tensor.
        end (int): End index for precomputing frequencies.
        theta (float, optional): Scaling factor for frequency computation. Defaults to 10000.0.

    Returns:
        torch.Tensor: Precomputed frequency tensor with complex exponentials.     

    """
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    """
    Reshape frequency tensor for broadcasting it with another tensor.

    This function reshapes the frequency tensor to have the same shape as the target tensor 'x'
    for the purpose of broadcasting the frequency tensor during element-wise operations.

    Args:
        freqs_cis (torch.Tensor): Frequency tensor to be reshaped.
        x (torch.Tensor): Target tensor for broadcasting compatibility.

    Returns:
        torch.Tensor: Reshaped frequency tensor.

    Raises:
        AssertionError: If the frequency tensor doesn't match the expected shape.
        AssertionError: If the target tensor 'x' doesn't have the expected number of dimensions.
    """
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)


def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cis: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Apply rotary embeddings to input tensors using the given frequency tensor.

    This function applies rotary embeddings to the given query 'xq' and key 'xk' tensors using the provided
    frequency tensor 'freqs_cis'. The input tensors are reshaped as complex numbers, and the frequency tensor
    is reshaped for broadcasting compatibility. The resulting tensors contain rotary embeddings and are
    returned as real tensors.

    Args:
        xq (torch.Tensor): Query tensor to apply rotary embeddings.
        xk (torch.Tensor): Key tensor to apply rotary embeddings.
        freqs_cis (torch.Tensor): Precomputed frequency tensor for complex exponentials.

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: Tuple of modified query tensor and key tensor with rotary embeddings.

        

    """
    xq_ = torch.view_as_complex(xq.float().reshape(*xq.shape[:-1], -1, 2))
    xk_ = torch.view_as_complex(xk.float().reshape(*xk.shape[:-1], -1, 2))
    freqs_cis = reshape_for_broadcast(freqs_cis, xq_)
    xq_out = torch.view_as_real(xq_ * freqs_cis).flatten(3)
    xk_out = torch.view_as_real(xk_ * freqs_cis).flatten(3)
    return xq_out.type_as(xq), xk_out.type_as(xk)

if __name__ == '__main__':
    torch.set_printoptions(linewidth=200)         # 这样打印不会存在折叠的问题
    # batch_size, seq_len, head_num, head_dim  = 1, 13, 32, 128
    batch_size, seq_len, head_num, head_dim  = 1, 6, 1, 8
    max_position_embeddings = 4096
    
    # test hf implementation
    cos_cached,sin_cached = init_rope_embeddings(dim=head_dim, max_position_embeddings=max_position_embeddings)
    
    xq = torch.randn(batch_size, head_num, seq_len, head_dim)
    import copy
    xk = copy.deepcopy(xq)
    # import pdb; pdb.set_trace()
    cos, sin = get_rope_embeddings(xq, seq_len=seq_len)
    position_ids = torch.arange(0, seq_len, dtype=torch.long).unsqueeze(0)
    hf_xq_new, hf_xk_new = apply_rotary_pos_emb(xq, xk, cos, sin, position_ids)
    # import pdb; pdb.set_trace()
    
    
    # test meta implementation
    xq_t = xq.transpose(1, 2)
    xk_t = xk.transpose(1, 2)
    # import pdb; pdb.set_trace()
    freqs_cis = precompute_freqs_cis(dim=head_dim, end=max_position_embeddings)
    freqs_cis = freqs_cis[:seq_len]
    meta_xq_new, meta_xk_new = apply_rotary_emb(xq_t, xk_t, freqs_cis)
    # import pdb; pdb.set_trace()
    meta_xq_new = meta_xq_new.transpose(1, 2)
    meta_xk_new = meta_xk_new.transpose(1, 2)
    
    error = torch.abs(meta_xq_new - hf_xq_new)
    print(f"Compare xq_new error sum: {torch.sum(error)}") 
    import pdb; pdb.set_trace()
    error = torch.abs(meta_xk_new - hf_xk_new)
    print(f"Compare xk_new error sum: {torch.sum(error)}") 

    
