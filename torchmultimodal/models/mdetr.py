# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from copy import deepcopy
from typing import Callable, Optional

import torch
import torch.nn.functional as F
from torch import nn, Tensor
from torchmultimodal.modules.encoders.mdetr_image_encoder import (
    mdetr_resnet101_backbone,
    PositionEmbeddingSine,
)
from torchmultimodal.modules.encoders.mdetr_text_encoder import mdetr_text_encoder
from torchmultimodal.utils.common import NestedTensor
from torchvision.models import resnet101

# TODO: add support for freezing text encoder in this class (?)
class MDETR(nn.Module):
    """This is the MDETR module that performs modulated object detection"""

    def __init__(
        self,
        image_backbone: nn.Module,
        text_encoder: nn.Module,
        transformer: nn.Module,
        pos_embed: nn.Module,
        resizer: nn.Module,
        input_proj: nn.Module,
        query_embed: nn.Module,
        bbox_embed: nn.Module,
        class_embed: nn.Module,
    ):
        """Initializes the model.

        Args:
            backbone: torch module of the backbone to be used. See backbone.py
            text_encoder: torch module of text encoder to be used
            transformer: torch module of the transformer architecture. See transformer.py

        """
        super().__init__()
        self.image_backbone = image_backbone
        self.text_encoder = text_encoder
        self.resizer = resizer
        self.transformer = transformer
        self.pos_embed = pos_embed
        self.input_proj = input_proj
        self.class_embed = class_embed
        self.bbox_embed = bbox_embed
        self.query_embed = query_embed

    def forward(self, images: NestedTensor, text: Tensor, text_attention_mask: Tensor):
        """The forward expects a NestedTensor of images, which consists of:
           - images.tensor: batched images, of shape [batch_size x 3 x H x W]
           - images.mask: a binary mask of shape [batch_size x H x W], containing 1 on padded pixels
        It also expects a tensor of texts and a tensor of text attention masks.

        It returns a dict with the following elements:
           - "pred_logits": the classification logits (including no-object) for all queries.
                            Shape= [batch_size x num_queries x (num_classes + 1)]
           - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                           (center_x, center_y, height, width). These values are normalized in [0, 1],
                           relative to the size of each individual image (disregarding possible padding).
                           See PostProcess for information on how to retrieve the unnormalized bounding box.
        """

        encoded_text = self.text_encoder(text, text_attention_mask)

        # Transpose memory because pytorch's attention expects sequence first
        text_memory = encoded_text.transpose(0, 1)

        # Invert attention mask that we get from huggingface because its the opposite in pytorch transformer
        text_attention_mask = text_attention_mask.ne(1).bool()

        features = self.image_backbone(images)
        pos = [self.pos_embed(x).to(x.tensors.dtype) for x in features]

        src, mask = features[-1].decompose()
        query_embed = self.query_embed.weight

        text_memory_resized = self.resizer(text_memory)

        hs = self.transformer(
            self.input_proj(src),
            mask,
            query_embed,
            pos[-1],
            text_memory=text_memory_resized,
            img_memory=None,
            text_attention_mask=text_attention_mask,
        )
        out = {}
        outputs_class = self.class_embed(hs)
        outputs_coord = self.bbox_embed(hs).sigmoid()

        out.update(
            {
                "pred_logits": outputs_class[-1],
                "pred_boxes": outputs_coord[-1],
            }
        )

        return out


class Transformer(nn.Module):
    def __init__(
        self,
        d_model=512,
        nhead=8,
        num_encoder_layers=6,
        num_decoder_layers=6,
        dim_feedforward=2048,
        dropout=0.1,
        activation: Callable[..., Tensor] = nn.functional.relu,
        normalize_before=False,
        return_intermediate_dec=False,
        pass_pos_and_query=True,
    ):
        super().__init__()

        self.pass_pos_and_query = pass_pos_and_query
        encoder_layer = TransformerEncoderLayer(
            d_model, nhead, dim_feedforward, dropout, activation, normalize_before
        )
        encoder_norm = nn.LayerNorm(d_model) if normalize_before else None
        self.encoder = TransformerEncoder(
            encoder_layer, num_encoder_layers, encoder_norm
        )

        decoder_layer = TransformerDecoderLayer(
            d_model, nhead, dim_feedforward, dropout, activation
        )
        decoder_norm = nn.LayerNorm(d_model)
        self.decoder = TransformerDecoder(
            decoder_layer,
            num_decoder_layers,
            decoder_norm,
            return_intermediate=return_intermediate_dec,
        )
        self.d_model = d_model
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(
        self,
        src=None,
        mask=None,
        query_embed=None,
        pos_embed=None,
        text_memory=None,
        img_memory=None,
        text_attention_mask=None,
    ):
        # flatten NxCxHxW to HWxNxC
        bs, _, _, _ = src.shape
        src = src.flatten(2).permute(2, 0, 1)
        pos_embed = pos_embed.flatten(2).permute(2, 0, 1)
        query_embed = query_embed.unsqueeze(1).repeat(1, bs, 1)
        mask = mask.flatten(1)

        if self.pass_pos_and_query:
            tgt = torch.zeros_like(query_embed)
        else:
            src, tgt, query_embed, pos_embed = (
                src + 0.1 * pos_embed,
                query_embed,
                None,
                None,
            )

        # Concat on the sequence dimension
        src = torch.cat([src, text_memory], dim=0)
        # For mask, sequence dimension is second
        mask = torch.cat([mask, text_attention_mask], dim=1)
        # Pad the pos_embed with 0 so that the addition will be a no-op for the text tokens
        pos_embed = torch.cat([pos_embed, torch.zeros_like(text_memory)], dim=0)

        img_memory = self.encoder(src, src_key_padding_mask=mask, pos=pos_embed)

        text_memory = img_memory[-len(text_memory) :]
        assert img_memory.shape[1] == text_memory.shape[1] == tgt.shape[1]

        # TODO: See about removing the duplication here
        src = None
        if self.pass_pos_and_query:
            tgt = torch.zeros_like(query_embed)
        else:
            src, tgt, query_embed, pos_embed = (
                src + 0.1 * pos_embed,
                query_embed,
                None,
                None,
            )

        assert img_memory.shape[1] == text_memory.shape[1] == tgt.shape[1]

        hs = self.decoder(
            tgt,
            img_memory,
            memory_key_padding_mask=mask,
            pos=pos_embed,
            query_pos=query_embed,
        )
        return hs.transpose(1, 2)


class TransformerEncoder(nn.Module):
    def __init__(self, encoder_layer, num_layers, norm=None):
        super().__init__()
        self.layers = _get_clones(encoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm

    def forward(
        self,
        src,
        mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
    ):

        output = src

        for layer in self.layers:
            output = layer(
                output,
                src_mask=mask,
                src_key_padding_mask=src_key_padding_mask,
                pos=pos,
            )

        if self.norm is not None:
            output = self.norm(output)

        return output


class TransformerDecoder(nn.Module):
    def __init__(self, decoder_layer, num_layers, norm=None, return_intermediate=False):
        super().__init__()
        self.layers = _get_clones(decoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm
        self.return_intermediate = return_intermediate

    def forward(
        self,
        tgt,
        memory,
        tgt_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        tgt_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):
        output = tgt

        intermediate = []

        for layer in self.layers:
            output = layer(
                output,
                memory,
                tgt_mask=tgt_mask,
                memory_mask=memory_mask,
                tgt_key_padding_mask=tgt_key_padding_mask,
                memory_key_padding_mask=memory_key_padding_mask,
                pos=pos,
                query_pos=query_pos,
            )
            if self.return_intermediate:
                intermediate.append(self.norm(output))

        if self.norm is not None:
            output = self.norm(output)
            if self.return_intermediate:
                intermediate.pop()
                intermediate.append(output)

        if self.return_intermediate:
            return torch.stack(intermediate)

        return output


class TransformerEncoderLayer(nn.Module):
    def __init__(
        self,
        d_model,
        nhead,
        dim_feedforward=2048,
        dropout=0.1,
        activation: Callable[..., Tensor] = nn.functional.relu,
        normalize_before=False,
    ):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

        self.activation = activation
        self.normalize_before = normalize_before

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(
        self,
        src,
        src_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
    ):
        q = k = self.with_pos_embed(src, pos)
        src2 = self.self_attn(
            q, k, value=src, attn_mask=src_mask, key_padding_mask=src_key_padding_mask
        )[0]
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src

    def forward_pre(
        self,
        src,
        src_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
    ):
        src2 = self.norm1(src)
        q = k = self.with_pos_embed(src2, pos)
        src2 = self.self_attn(
            q, k, value=src2, attn_mask=src_mask, key_padding_mask=src_key_padding_mask
        )[0]
        src = src + self.dropout1(src2)
        src2 = self.norm2(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src2))))
        src = src + self.dropout2(src2)
        return src

    def forward(
        self,
        src,
        src_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
    ):
        if self.normalize_before:
            return self.forward_pre(src, src_mask, src_key_padding_mask, pos)
        return self.forward_post(src, src_mask, src_key_padding_mask, pos)


class TransformerDecoderLayer(nn.Module):
    def __init__(
        self,
        d_model,
        nhead,
        dim_feedforward=2048,
        dropout=0.1,
        activation: Callable[..., Tensor] = nn.functional.relu,
    ):
        super().__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
        self.cross_attn_image = nn.MultiheadAttention(d_model, nhead, dropout=dropout)

        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)

        self.norm3 = nn.LayerNorm(d_model)
        self.norm4 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)

        self.dropout3 = nn.Dropout(dropout)
        self.dropout4 = nn.Dropout(dropout)

        self.activation = activation

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward(
        self,
        tgt,
        memory,
        tgt_mask: Optional[Tensor] = None,
        memory_mask: Optional[Tensor] = None,
        tgt_key_padding_mask: Optional[Tensor] = None,
        memory_key_padding_mask: Optional[Tensor] = None,
        pos: Optional[Tensor] = None,
        query_pos: Optional[Tensor] = None,
    ):
        q = k = self.with_pos_embed(tgt, query_pos)

        # Self attention
        tgt2 = self.self_attn(
            q, k, value=tgt, attn_mask=tgt_mask, key_padding_mask=tgt_key_padding_mask
        )[0]
        tgt = tgt + self.dropout1(tgt2)
        tgt = self.norm1(tgt)

        # Cross attention to image
        tgt2 = self.cross_attn_image(
            query=self.with_pos_embed(tgt, query_pos),
            key=self.with_pos_embed(memory, pos),
            value=memory,
            attn_mask=memory_mask,
            key_padding_mask=memory_key_padding_mask,
        )[0]
        tgt = tgt + self.dropout3(tgt2)
        tgt = self.norm3(tgt)

        # FFN
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt))))
        tgt = tgt + self.dropout4(tgt2)
        tgt = self.norm4(tgt)
        return tgt


class FeatureResizer(nn.Module):
    """
    This class takes as input a set of embeddings of dimension C1 and outputs a set of
    embedding of dimension C2, after a linear transformation, dropout and normalization (LN).
    """

    def __init__(
        self,
        input_feat_size: int,
        output_feat_size: int,
        dropout: float = 0.1,
        do_ln=True,
    ):
        super().__init__()
        self.do_ln = do_ln
        # Object feature encoding
        self.fc = nn.Linear(input_feat_size, output_feat_size, bias=True)
        self.layer_norm = nn.LayerNorm(output_feat_size, eps=1e-12)
        self.dropout = nn.Dropout(dropout)

    def forward(self, encoder_features):
        x = self.fc(encoder_features)
        if self.do_ln:
            x = self.layer_norm(x)
        output = self.dropout(x)
        return output


def _get_clones(module, N):
    return nn.ModuleList([deepcopy(module) for i in range(N)])


# TODO: replace with TorchMultimodal MLP
class MLP(nn.Module):
    """Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(
            nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim])
        )

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


def mdetr_transformer() -> Transformer:
    return Transformer(
        d_model=256,
        nhead=8,
        num_encoder_layers=6,
        num_decoder_layers=6,
        dim_feedforward=2048,
        dropout=0.1,
        return_intermediate_dec=True,
        pass_pos_and_query=True,
    )


def mdetr_resnet101(num_queries: int = 100, num_classes: int = 255) -> MDETR:
    image_backbone = resnet101()
    image_backbone = mdetr_resnet101_backbone()
    pos_embed = PositionEmbeddingSine(128, normalize=True)
    image_backbone.num_channels = 2048
    text_encoder = mdetr_text_encoder()
    transformer = mdetr_transformer()
    embedding_dim = text_encoder.encoder.embedding_dim
    hidden_dim = transformer.d_model
    resizer = FeatureResizer(embedding_dim, hidden_dim)
    input_proj = nn.Conv2d(image_backbone.num_channels, hidden_dim, kernel_size=1)
    query_embed = nn.Embedding(num_queries, hidden_dim)
    bbox_embed = MLP(hidden_dim, hidden_dim, 4, 3)
    class_embed = nn.Linear(hidden_dim, num_classes + 1)
    mdetr = MDETR(
        image_backbone,
        text_encoder,
        transformer,
        pos_embed,
        resizer,
        input_proj,
        query_embed,
        bbox_embed,
        class_embed,
    )
    return mdetr
