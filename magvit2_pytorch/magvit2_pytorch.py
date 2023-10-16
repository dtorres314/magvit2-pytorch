import copy
from math import log2, ceil
from functools import wraps

import torch
import torch.nn.functional as F
from torch import nn, einsum, Tensor
from torch.nn import Module, ModuleList

import torchvision

from collections import namedtuple

from vector_quantize_pytorch import LFQ

from einops import rearrange, repeat, reduce, pack, unpack
from einops.layers.torch import Rearrange

from beartype import beartype
from beartype.typing import Union, Tuple, Optional

# helper

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d

def identity(t):
    return t

def divisible_by(num, den):
    return (num % den) == 0

def pack_one(t, pattern):
    return pack([t], pattern)

def unpack_one(t, ps, pattern):
    return unpack(t, ps, pattern)[0]

def is_odd(n):
    return not divisible_by(n, 2)

def cast_tuple(t, length = 1):
    return t if isinstance(t, tuple) else ((t,) * length)

# tensor helpers

def pad_at_dim(t, pad, dim = -1, value = 0.):
    dims_from_right = (- dim - 1) if dim < 0 else (t.ndim - dim - 1)
    zeros = ((0, 0) * dims_from_right)
    return F.pad(t, (*zeros, *pad), value = value)

def pick_video_frame(video, frame_indices):
    batch, device = video.shape[0], video.device
    video = rearrange(video, 'b c f ... -> b f c ...')
    batch_indices = torch.arange(batch, device = device)
    batch_indices = rearrange(batch_indices, 'b -> b 1')
    images = video[batch_indices, frame_indices]
    images = rearrange(images, 'b 1 c ... -> b c ...')
    return images

# helper decorators

def remove_vgg(fn):
    @wraps(fn)
    def inner(self, *args, **kwargs):
        has_vgg = hasattr(self, 'vgg')
        if has_vgg:
            vgg = self.vgg
            delattr(self, 'vgg')

        out = fn(self, *args, **kwargs)

        if has_vgg:
            self.vgg = vgg

        return out
    return inner

# helper classes

def Sequential(*modules):
    modules = [*filter(exists, modules)]

    if len(modules) == 0:
        return nn.Identity()

    return nn.Sequential(*modules)

class Residual(Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x

# adaptive conv from Karras et al. Stylegan2
# for conditioning on latents

class AdaptiveConv3DMod(Module):
    @beartype
    def __init__(
        self,
        dim,
        *,
        spatial_kernel,
        time_kernel,
        dim_out = None,
        demod = True,
        eps = 1e-8,
    ):
        super().__init__()
        dim_out = default(dim_out, dim)

        self.eps = eps

        assert is_odd(spatial_kernel) and is_odd(time_kernel)

        self.spatial_kernel = spatial_kernel
        self.time_kernel = time_kernel

        self.padding = (*((spatial_kernel // 2,) * 4), time_kernel - 1, 0)
        self.weights = nn.Parameter(torch.randn((dim_out, dim, time_kernel, spatial_kernel, spatial_kernel)))

        self.demod = demod

        nn.init.kaiming_normal_(self.weights, a = 0, mode = 'fan_in', nonlinearity = 'selu')

    def forward(
        self,
        fmap,
        mod: Optional[Tensor] = None
    ):
        """
        notation

        b - batch
        n - convs
        o - output
        i - input
        k - kernel
        """

        b = fmap.shape[0]

        # prepare weights for modulation

        weights = self.weights

        # do the modulation, demodulation, as done in stylegan2

        mod = rearrange(mod, 'b i -> b 1 i 1 1 1')

        weights = weights * (mod + 1)

        if self.demod:
            inv_norm = reduce(weights ** 2, 'b o i k0 k1 k2 -> b o 1 1 1 1', 'sum').clamp(min = self.eps).rsqrt()
            weights = weights * inv_norm

        fmap = rearrange(fmap, 'b c t h w -> 1 (b c) t h w')

        weights = rearrange(weights, 'b o ... -> (b o) ...')

        fmap = F.pad(fmap, self.padding)
        fmap = F.conv3d(fmap, weights, groups = b)

        return rearrange(fmap, '1 (b o) ... -> b o ...', b = b)

# strided conv downsamples

class SpatialDownsample2x(Module):
    def __init__(
        self,
        dim,
        dim_out = None,
        kernel_size = 3
    ):
        super().__init__()
        dim_out = default(dim_out, dim)
        self.conv = nn.Conv2d(dim, dim_out, kernel_size, stride = 2, padding = kernel_size // 2)

    def forward(self, x):
        x = rearrange(x, 'b c t h w -> b t c h w')
        x, ps = pack_one(x, '* c h w')

        out = self.conv(x)

        out = unpack_one(out, ps, '* c h w')
        out = rearrange(out, 'b t c h w -> b c t h w')
        return out

class TimeDownsample2x(Module):
    def __init__(
        self,
        dim,
        dim_out = None,
        kernel_size = 3
    ):
        super().__init__()
        dim_out = default(dim_out, dim)
        self.conv = nn.Conv1d(dim, dim_out, kernel_size, stride = 2, padding = kernel_size // 2)

    def forward(self, x):
        x = rearrange(x, 'b c t h w -> b h w c t')
        x, ps = pack_one(x, '* c t')

        out = self.conv(x)

        out = unpack_one(out, ps, '* c t')
        out = rearrange(out, 'b h w c t -> b c t h w')
        return out

# depth to space upsamples

class SpatialUpsample2x(Module):
    def __init__(
        self,
        dim,
        dim_out = None
    ):
        super().__init__()
        dim_out = default(dim_out, dim)
        conv = nn.Conv2d(dim, dim_out * 4, 1)

        self.net = nn.Sequential(
            conv,
            nn.SiLU(),
            Rearrange('b (c p1 p2) h w -> b c (h p1) (w p2)', p1 = 2, p2 = 2)
        )

        self.init_conv_(conv)

    def init_conv_(self, conv):
        o, i, h, w = conv.weight.shape
        conv_weight = torch.empty(o // 4, i, h, w)
        nn.init.kaiming_uniform_(conv_weight)
        conv_weight = repeat(conv_weight, 'o ... -> (o 4) ...')

        conv.weight.data.copy_(conv_weight)
        nn.init.zeros_(conv.bias.data)

    def forward(self, x):
        x = rearrange(x, 'b c t h w -> b t c h w')
        x, ps = pack_one(x, '* c h w')

        out = self.net(x)

        out = unpack_one(out, ps, '* c h w')
        out = rearrange(out, 'b t c h w -> b c t h w')
        return out

class TimeUpsample2x(Module):
    def __init__(
        self,
        dim,
        dim_out = None
    ):
        super().__init__()
        dim_out = default(dim_out, dim)
        conv = nn.Conv1d(dim, dim_out * 2, 1)

        self.net = nn.Sequential(
            conv,
            nn.SiLU(),
            Rearrange('b (c p) t -> b c (t p)', p = 2)
        )

        self.init_conv_(conv)

    def init_conv_(self, conv):
        o, i, t = conv.weight.shape
        conv_weight = torch.empty(o // 2, i, t)
        nn.init.kaiming_uniform_(conv_weight)
        conv_weight = repeat(conv_weight, 'o ... -> (o 2) ...')

        conv.weight.data.copy_(conv_weight)
        nn.init.zeros_(conv.bias.data)

    def forward(self, x):
        x = rearrange(x, 'b c t h w -> b h w c t')
        x, ps = pack_one(x, '* c t')

        out = self.net(x)

        out = unpack_one(out, ps, '* c t')
        out = rearrange(out, 'b h w c t -> b c t h w')
        return out

# autoencoder - only best variant here offered, with causal conv 3d

class CausalConv3d(Module):
    @beartype
    def __init__(
        self,
        chan_in,
        chan_out,
        kernel_size: Union[int, Tuple[int, int, int]],
        pad_mode = 'reflect',
        **kwargs
    ):
        super().__init__()
        kernel_size = cast_tuple(kernel_size, 3)

        time_kernel_size, height_kernel_size, width_kernel_size = kernel_size

        assert is_odd(height_kernel_size) and is_odd(width_kernel_size)

        dilation = kwargs.pop('dilation', 1)
        stride = kwargs.pop('stride', 1)

        self.pad_mode = pad_mode
        time_pad = dilation * (time_kernel_size - 1) + (1 - stride)
        height_pad = height_kernel_size // 2
        width_pad = width_kernel_size // 2

        self.time_pad = time_pad
        self.time_causal_padding = (width_pad, width_pad, height_pad, height_pad, time_pad, 0)

        stride = (stride, 1, 1)
        dilation = (dilation, 1, 1)
        self.conv = nn.Conv3d(chan_in, chan_out, kernel_size, stride = stride, dilation = dilation, **kwargs)

    def forward(self, x):
        pad_mode = self.pad_mode if self.time_pad < x.shape[2] else 'constant'

        x = F.pad(x, self.time_causal_padding, mode = pad_mode)
        return self.conv(x)

@beartype
def ResidualUnit(
    dim,
    kernel_size: Union[int, Tuple[int, int, int]],
    pad_mode: str = 'reflect'
):
    return Residual(Sequential(
        CausalConv3d(dim, dim, kernel_size, pad_mode = pad_mode),
        nn.ELU(),
        CausalConv3d(dim, dim, 1, pad_mode = pad_mode),
        nn.ELU()
    ))

class CausalConvTranspose3d(Module):
    def __init__(
        self,
        chan_in,
        chan_out,
        kernel_size: Union[int, Tuple[int, int, int]],
        *,
        time_stride,
        **kwargs
    ):
        super().__init__()
        kernel_size = cast_tuple(kernel_size, 3)

        time_kernel_size, height_kernel_size, width_kernel_size = kernel_size

        assert is_odd(height_kernel_size) and is_odd(width_kernel_size)

        self.upsample_factor = time_stride

        height_pad = height_kernel_size // 2
        width_pad = width_kernel_size // 2

        stride = (time_stride, 1, 1)
        padding = (0, height_pad, width_pad)

        self.conv = nn.ConvTranspose3d(chan_in, chan_out, kernel_size, stride, padding = padding, **kwargs)

    def forward(self, x):
        assert x.ndim == 5
        t = x.shape[2]

        out = self.conv(x)

        out = out[..., :(t * self.upsample_factor), :, :]
        return out

# video tokenizer class

LossBreakdown = namedtuple('LossBreakdown', [
    'recon_loss',
    'lfq_aux_losses',
    'perceptual_loss'
])

class VideoTokenizer(Module):
    @beartype
    def __init__(
        self,
        layers: Tuple[Tuple[str, int], ...] = (
            ('residual', 64),
            ('residual', 64),
            ('residual', 64)
        ),
        residual_conv_kernel_size = 3,
        num_codebooks = 1,
        codebook_size = 8192,
        channels = 3,
        init_dim = 64,
        input_conv_kernel_size: Tuple[int, int, int] = (7, 7, 7),
        output_conv_kernel_size: Tuple[int, int, int] = (3, 3, 3),
        pad_mode: str = 'reflect',
        lfq_entropy_loss_weight = 0.1,
        lfq_commitment_loss_weight = 1.,
        lfq_diversity_gamma = 1.,
        vgg: Optional[Module] = None,
        perceptual_loss_weight = 1.

    ):
        super().__init__()

        # encoder

        self.conv_in = CausalConv3d(channels, init_dim, input_conv_kernel_size, pad_mode = pad_mode)

        self.encoder_layers = ModuleList([])
        self.decoder_layers = ModuleList([])

        self.conv_out = CausalConv3d(init_dim, channels, output_conv_kernel_size, pad_mode = pad_mode)

        dim = init_dim
        time_downsample_factor = 1

        for layer_type, dim_out in layers:
            if layer_type == 'residual':
                assert dim == dim_out

                encoder_layer = ResidualUnit(dim, residual_conv_kernel_size)
                decoder_layer = ResidualUnit(dim, residual_conv_kernel_size)

            elif layer_type == 'compress_space':
                encoder_layer = SpatialDownsample2x(dim, dim_out)
                decoder_layer = SpatialUpsample2x(dim_out, dim)

            elif layer_type == 'compress_time':
                encoder_layer = TimeDownsample2x(dim, dim_out)
                decoder_layer = TimeUpsample2x(dim_out, dim)

                time_downsample_factor *= 2
            else:
                raise ValueError(f'unknown layer type {layer_type}')

            self.encoder_layers.append(encoder_layer)
            self.decoder_layers.insert(0, decoder_layer)

            dim = dim_out

        self.time_downsample_factor = time_downsample_factor
        self.time_padding = time_downsample_factor - 1

        # lookup free quantizer(s) - multiple codebooks is possible
        # each codebook will get its own entropy regularization

        self.quantizers = LFQ(
            dim = dim,
            codebook_size = codebook_size,
            num_codebooks = num_codebooks,
            entropy_loss_weight = lfq_entropy_loss_weight,
            commitment_loss_weight = lfq_commitment_loss_weight,
            diversity_gamma = lfq_diversity_gamma
        )

        # dummy loss

        self.register_buffer('zero', torch.zeros(1,), persistent = False)

        # perceptual loss related

        use_vgg = channels == 3 and perceptual_loss_weight > 0.

        self.vgg = None
        self.perceptual_loss_weight = perceptual_loss_weight

        if use_vgg:
            if not exists(vgg):
                vgg = torchvision.models.vgg16(pretrained = True)
                vgg.classifier = nn.Sequential(*vgg.classifier[:-2])

            self.vgg = vgg

        self.use_vgg = use_vgg

    def copy_for_eval(self):
        device = next(self.parameters()).device
        vae_copy = copy.deepcopy(self.cpu())

        if vae_copy.use_vgg_and_gan:
            del vae_copy.discr
            del vae_copy.vgg

        vae_copy.eval()
        return vae_copy.to(device)

    @remove_vgg
    def state_dict(self, *args, **kwargs):
        return super().state_dict(*args, **kwargs)

    @remove_vgg
    def load_state_dict(self, *args, **kwargs):
        return super().load_state_dict(*args, **kwargs)

    def load(self, path):
        path = Path(path)
        assert path.exists()
        pt = torch.load(str(path))
        self.load_state_dict(pt)

    @beartype
    def encode(
        self,
        video: Tensor,
        quantize = False
    ):
        x = self.conv_in(video)

        for fn in self.encoder_layers:
            x = fn(x)

        maybe_quantize = identity if not quantize else self.quantizers

        return maybe_quantize(x)

    @beartype
    def decode(self, codes: Tensor):
        x = codes

        for fn in self.decoder_layers:
            x = fn(x)

        return self.conv_out(x)

    @beartype
    def forward(
        self,
        video_or_images: Tensor,
        return_loss = False,
        return_codes = False
    ):
        assert not (return_loss and return_codes)
        assert video_or_images.ndim in {4, 5}

        # accept images for image pretraining (curriculum learning from images to video)

        if video_or_images.ndim == 4:
            video = rearrange(video, 'b c ... -> b c 1 ...')
        else:
            video = video_or_images

        batch, frames = video.shape[0], video.shape[2]

        assert divisible_by(frames - 1, self.time_downsample_factor), f'number of frames {frames} minus the first frame ({frames - 1}) must be divisible by the total downsample factor across time {self.time_downsample_factor}'

        # pad the time, accounting for total time downsample factor, so that images can be trained independently

        padded_video = pad_at_dim(video, (self.time_padding, 0), value = 0., dim = 2)

        # encoder

        x = self.encode(padded_video)

        # lookup free quantization

        quantized, codes, aux_losses = self.quantizers(x)

        if return_codes:
            return codes

        # decoder

        padded_recon_video = self.decode(quantized)

        recon_video = padded_recon_video[:, :, self.time_padding:]

        # reconstruction loss

        if not return_loss:
            return recon_video

        recon_loss = F.mse_loss(video, recon_video)

        # perceptual loss

        if self.use_vgg:
            frame_indices = torch.randn((batch, frames)).topk(1, dim = -1).indices

            input_vgg_input = pick_video_frame(video, frame_indices)
            recon_vgg_input = pick_video_frame(recon_video, frame_indices)

            input_vgg_feats = self.vgg(input_vgg_input)
            recon_vgg_feats = self.vgg(recon_vgg_input)

            perceptual_loss = F.mse_loss(input_vgg_feats, recon_vgg_feats)
        else:
            perceptual_loss = self.zero

        total_loss = recon_loss + aux_losses + perceptual_loss * self.perceptual_loss_weight

        return total_loss, LossBreakdown(recon_loss, aux_losses, perceptual_loss)

# main class

class MagViT2(Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x
