"""
Copyright (C) 2019 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from models.networks.architecture import VGG19


# Defines the GAN loss which uses either LSGAN or the regular GAN.
# When LSGAN is used, it is basically same as MSELoss,
# but it abstracts away the need to create the target label tensor
# that has the same size as the input
class GANLoss(nn.Module):
    def __init__(self, gan_mode, target_real_label=1.0, target_fake_label=0.0,
                 tensor=torch.FloatTensor, opt=None):
        super(GANLoss, self).__init__()
        self.real_label = target_real_label
        self.fake_label = target_fake_label
        self.real_label_tensor = None
        self.fake_label_tensor = None
        self.zero_tensor = None
        self.Tensor = tensor
        self.gan_mode = gan_mode
        self.opt = opt
        if gan_mode == 'ls':
            pass
        elif gan_mode == 'original':
            pass
        elif gan_mode == 'w':
            pass
        elif gan_mode == 'hinge':
            pass
        elif gan_mode == 'conv_d':
            from exp.spade.unified_loss import ConvUnifiedLoss, downsampling
            from template_lib.v2.config_cfgnode import global_cfg
            self.unified_loss = global_cfg.unified_loss
            self.d_loss = ConvUnifiedLoss()
            self.downsample = downsampling
            self.out_channel = global_cfg.D_cfg.get('out_channel')
        else:
            raise ValueError('Unexpected gan_mode {}'.format(gan_mode))

    def get_target_tensor(self, input, target_is_real):
        if target_is_real:
            if self.real_label_tensor is None:
                self.real_label_tensor = self.Tensor(1).fill_(self.real_label)
                self.real_label_tensor.requires_grad_(False)
            return self.real_label_tensor.expand_as(input)
        else:
            if self.fake_label_tensor is None:
                self.fake_label_tensor = self.Tensor(1).fill_(self.fake_label)
                self.fake_label_tensor.requires_grad_(False)
            return self.fake_label_tensor.expand_as(input)

    def get_zero_tensor(self, input):
        if self.zero_tensor is None:
            self.zero_tensor = self.Tensor(1).fill_(0)
            self.zero_tensor.requires_grad_(False)
        return self.zero_tensor.expand_as(input)

    def loss(self, input, target_is_real, for_discriminator=True, **kwargs):
        if self.gan_mode == 'original':  # cross entropy loss
            target_tensor = self.get_target_tensor(input, target_is_real)
            loss = F.binary_cross_entropy_with_logits(input, target_tensor)
            return loss
        elif self.gan_mode == 'ls':
            target_tensor = self.get_target_tensor(input, target_is_real)
            return F.mse_loss(input, target_tensor)
        elif self.gan_mode == 'hinge':
            if for_discriminator:
                if target_is_real:
                    minval = torch.min(input - 1, self.get_zero_tensor(input))
                    loss = -torch.mean(minval)
                else:
                    minval = torch.min(-input - 1, self.get_zero_tensor(input))
                    loss = -torch.mean(minval)
            else:
                assert target_is_real, "The generator's hinge loss must be aiming for real"
                loss = -torch.mean(input)
            return loss
        elif self.gan_mode == 'conv_d':
            input_semantics = kwargs['input_semantics']
            input_semantics = self.downsample(input_semantics, size=input.shape[-2:])
            _, labels = input_semantics.topk(k=1, dim=1)
            labels = labels.squeeze(1)
            if for_discriminator:
                if target_is_real:
                    D_real_positive = [labels, self.out_channel - 2]
                    if self.unified_loss.mode.lower() == "only_p":
                        loss = self.d_loss(pred=input, positive=D_real_positive, default_label=-1)
                    elif self.unified_loss.mode.lower() == "p_and_n":
                        loss = self.d_loss(pred=input, positive=D_real_positive, default_label=0)
                    else:
                        assert 0
                else:
                    D_fake_positive = (self.out_channel - 1, )
                    if self.unified_loss.mode.lower() == "only_p":
                        loss = self.d_loss(pred=input, positive=D_fake_positive, default_label=-1)
                    elif self.unified_loss.mode.lower() == "p_and_n":
                        loss = self.d_loss(pred=input, positive=D_fake_positive, default_label=0)
                    else:
                        assert 0
            else:
                assert target_is_real, "The generator's hinge loss must be aiming for real"
                G_positive = (labels, self.out_channel - 2)
                if self.unified_loss.mode.lower() == "only_p":
                    loss = self.d_loss(pred=input, positive=G_positive, default_label=-1)
                elif self.unified_loss.mode.lower() == "p_and_n":
                    # G_negative = (self.out_channel - 1, )
                    loss = self.d_loss(pred=input, positive=G_positive, default_label=0)
                else:
                    assert 0
            return loss
        else:
            # wgan
            if target_is_real:
                return -input.mean()
            else:
                return input.mean()

    def __call__(self, input, target_is_real, for_discriminator=True, **kwargs):
        # computing loss is a bit complicated because |input| may not be
        # a tensor, but list of tensors in case of multiscale discriminator
        if isinstance(input, list):
            loss = 0
            for pred_i in input:
                if isinstance(pred_i, list):
                    pred_i = pred_i[-1]
                loss_tensor = self.loss(pred_i, target_is_real, for_discriminator, **kwargs)
                bs = 1 if len(loss_tensor.size()) == 0 else loss_tensor.size(0)
                new_loss = torch.mean(loss_tensor.view(bs, -1), dim=1)
                loss += new_loss
            return loss / len(input)
        else:
            return self.loss(input, target_is_real, for_discriminator)


# Perceptual loss that uses a pretrained VGG network
class VGGLoss(nn.Module):
    def __init__(self, gpu_ids):
        super(VGGLoss, self).__init__()
        self.vgg = VGG19().cuda()
        self.criterion = nn.L1Loss()
        self.weights = [1.0 / 32, 1.0 / 16, 1.0 / 8, 1.0 / 4, 1.0]

    def forward(self, x, y):
        x_vgg, y_vgg = self.vgg(x), self.vgg(y)
        loss = 0
        for i in range(len(x_vgg)):
            loss += self.weights[i] * self.criterion(x_vgg[i], y_vgg[i].detach())
        return loss


# KL Divergence loss used in VAE with an image encoder
class KLDLoss(nn.Module):
    def forward(self, mu, logvar):
        return -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
