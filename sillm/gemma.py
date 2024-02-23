from functools import partial

import mlx.core as mx
import mlx.nn as nn

from sillm.model import BaseModel
from sillm.args import ModelArgs
import sillm.llama as llama

@partial(mx.compile, shapeless=True)
def rms_norm(x, weight, eps):
    x = x.astype(mx.float32)
    x = x * mx.rsqrt(x.square().mean(-1, keepdims=True) + eps)
    
    return (1.0 + weight) * x.astype(weight.dtype)

class RMSNorm(nn.Module):
    """
    Root Mean Square Normalization module.
    """
    def __init__(self,
                 dims: int,
                 eps: float = 1e-6):
        super().__init__()
        self.weight = mx.ones((dims,))
        self.eps = eps

    def __call__(self, x):
        return rms_norm(x, self.weight, self.eps)
    
class FeedForward(llama.FeedForward):
    """
    Feed-forward module.
    """
    def __call__(self, x) -> mx.array:
        """
        Args:
            x: Input tensor.
        Returns:
            Output tensor.
        """
        return self.w2(nn.gelu(self.w1(x)) * self.w3(x))
    
class TransformerBlock(llama.TransformerBlock):
    """
    Transformer block.
    """
    def __init__(self, args: ModelArgs):
        """
        Args:
            args: Model arguments.
        """
        nn.Module.__init__(self)

        self.n_heads = args.n_heads
        self.dim = args.dim
        self.attention = llama.Attention(args)
        self.feed_forward = FeedForward(args=args)
        self.attention_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.ffn_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.args = args

########
# References:
# https://github.com/huggingface/transformers/blob/main/src/transformers/models/gemma/modeling_gemma.py
########
class Model(llama.Model):
    """
    Gemma model wrapper.
    """
    def __init__(self, args: ModelArgs):
        """
        Args:
            args: Model arguments.
        """
        BaseModel.__init__(self, args)
        self.args = args

        self.n_layers = args.n_layers
        self.vocab_size = args.vocab_size
        
        self.tok_embeddings = nn.Embedding(args.vocab_size, args.dim)
        self.layers = [TransformerBlock(args=args) for _ in range(args.n_layers)]
        self.norm = RMSNorm(args.dim, eps=args.norm_eps)

    def __call__(self,
                 inputs: mx.array,
                 cache = None
                 ):
        """
        Args:
            inputs: Input tokens.
            cache: Cache from previous forward pass.
        Returns:
            Output logits and cache.
        """
        h = self.tok_embeddings(inputs)
        h = h * (self.args.dim**0.5)

        mask = None
        if h.shape[1] > 1:
            mask = nn.MultiHeadAttention.create_additive_causal_mask(h.shape[1])
            mask = mask.astype(h.dtype)

        if cache is None:
            cache = [None] * len(self.layers)

        for e, layer in enumerate(self.layers):
            h, cache[e] = layer(h, mask, cache[e])

        out = self.norm(h) @ self.tok_embeddings.weight.T

        return out, cache