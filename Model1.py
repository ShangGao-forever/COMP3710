# -*- coding: utf-8 -*-
"""
Created on Fri Oct 29 05:40:36 2021

@author: shane
"""

# Import necessary modules
import torch
import torch.nn as nn
import math

# Constraints
# Input: [batch_size, in_channels, height, width]

# Scaled weight - He initialization
# "explicitly scale the weights at runtime"

class ScaleW:
    '''
    Constructor: name - name of attribute to be scaled
    '''
    def __init__(self, name):
        self.name = name
    
    def scale(self, module):
        weight = getattr(module, self.name + '_orig')
        fan_in = weight.data.size(1) * weight.data[0][0].numel()
        
        return weight * math.sqrt(2 / fan_in)
    
    @staticmethod
    def apply(module, name):
        '''
        Apply runtime scaling to specific module
        '''
        hook = ScaleW(name)
        weight = getattr(module, name)
        module.register_parameter(name + '_orig', nn.Parameter(weight.data))
        del module._parameters[name]
        module.register_forward_pre_hook(hook)
    
    def __call__(self, module, whatever):
        weight = self.scale(module)
        setattr(module, self.name, weight)

# Quick apply for scaled weight
def quick_scale(module, name='weight'):
    ScaleW.apply(module, name)
    return module

# Uniformly set the hyperparameters of Linears
# "We initialize all weights of the convolutional, fully-connected, and affine transform layers using N(0, 1)"
# 5/13: Apply scaled weights
class SLinear(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()

        linear = nn.Linear(dim_in, dim_out)
        linear.weight.data.normal_()
        linear.bias.data.zero_()
        
        self.linear = quick_scale(linear)

    def forward(self, x):
        return self.linear(x)

# Uniformly set the hyperparameters of Conv2d
# "We initialize all weights of the convolutional, fully-connected, and affine transform layers using N(0, 1)"
# 5/13: Apply scaled weights
class SConv2d(nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()

        conv = nn.Conv2d(*args, **kwargs)
        conv.weight.data.normal_()
        conv.bias.data.zero_()
        
        self.conv = quick_scale(conv)

    def forward(self, x):
        return self.conv(x)

# Normalization on every element of input vector
class PixelNorm(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x / torch.sqrt(torch.mean(x ** 2, dim=1, keepdim=True) + 1e-8)

# "learned affine transform" A
class FC_A(nn.Module):
    '''
    Learned affine transform A, this module is used to transform
    midiate vector w into a style vector
    '''
    def __init__(self, dim_latent, n_channel):
        super().__init__()
        self.transform = SLinear(dim_latent, n_channel * 2)
        # "the biases associated with ys that we initialize to one"
        self.transform.linear.bias.data[:n_channel] = 1
        self.transform.linear.bias.data[n_channel:] = 0

    def forward(self, w):
        # Gain scale factor and bias with:
        style = self.transform(w).unsqueeze(2).unsqueeze(3)
        return style
    
# AdaIn (AdaptiveInstanceNorm)
class AdaIn(nn.Module):
    '''
    adaptive instance normalization
    '''
    def __init__(self, n_channel):
        super().__init__()
        self.norm = nn.InstanceNorm2d(n_channel)
        
    def forward(self, image, style):
        factor, bias = style.chunk(2, 1)
        result = self.norm(image)
        result = result * factor + bias  
        return result

# "learned per-channel scaling factors" B
# 5/13: Debug - tensor -> nn.Parameter
class Scale_B(nn.Module):
    '''
    Learned per-channel scale factor, used to scale the noise
    '''
    def __init__(self, n_channel):
        super().__init__()
        self.weight = nn.Parameter(torch.zeros((1, n_channel, 1, 1)))
    
    def forward(self, noise):
        result = noise * self.weight
        return result 

# Early convolutional block
# 5/13: Debug - tensor -> nn.Parameter
# 5/13: Remove noise generating module
class Early_StyleConv_Block(nn.Module):
    '''
    This is the very first block of generator that get the constant value as input
    '''
    def __init__ (self, n_channel, dim_latent, dim_input):
        super().__init__()
        # Constant input
        self.constant = nn.Parameter(torch.randn(1, n_channel, dim_input, dim_input))
        # Style generators
        self.style1   = FC_A(dim_latent, n_channel)
        self.style2   = FC_A(dim_latent, n_channel)
        # Noise processing modules
        self.noise1   = quick_scale(Scale_B(n_channel))
        self.noise2   = quick_scale(Scale_B(n_channel))
        # AdaIn
        self.adain    = AdaIn(n_channel)
        self.lrelu    = nn.LeakyReLU(0.2)
        # Convolutional layer
        self.conv     = SConv2d(n_channel, n_channel, 3, padding=1)
    
    def forward(self, latent_w, noise):
        # Gaussian Noise: Proxyed by generator
        # noise1 = torch.normal(mean=0,std=torch.ones(self.constant.shape)).cuda()
        # noise2 = torch.normal(mean=0,std=torch.ones(self.constant.shape)).cuda()
        result = self.constant.repeat(noise.shape[0], 1, 1, 1)
        result = result + self.noise1(noise)
        result = self.adain(result, self.style1(latent_w))
        result = self.lrelu(result)
        result = self.conv(result)
        result = result + self.noise2(noise)
        result = self.adain(result, self.style2(latent_w))
        result = self.lrelu(result)
        
        return result
    
# General convolutional blocks
# 5/13: Remove upsampling
# 5/13: Remove noise generating
class StyleConv_Block(nn.Module):
    '''
    This is the general class of style-based convolutional blocks
    '''
    def __init__ (self, in_channel, out_channel, dim_latent):
        super().__init__()
        # Style generators
        self.style1   = FC_A(dim_latent, out_channel)
        self.style2   = FC_A(dim_latent, out_channel)
        # Noise processing modules
        self.noise1   = quick_scale(Scale_B(out_channel))
        self.noise2   = quick_scale(Scale_B(out_channel))
        # AdaIn
        self.adain    = AdaIn(out_channel)
        self.lrelu    = nn.LeakyReLU(0.2)
        # Convolutional layers
        self.conv1    = SConv2d(in_channel, out_channel, 3, padding=1)
        self.conv2    = SConv2d(out_channel, out_channel, 3, padding=1)
    
    def forward(self, previous_result, latent_w, noise):
        # Upsample: Proxyed by generator
        # result = nn.functional.interpolate(previous_result, scale_factor=2, mode='bilinear',
        #                                           align_corners=False)
        # Conv 3*3
        result = self.conv1(previous_result)
        # Gaussian Noise: Proxyed by generator
        # noise1 = torch.normal(mean=0,std=torch.ones(result.shape)).cuda()
        # noise2 = torch.normal(mean=0,std=torch.ones(result.shape)).cuda()
        # Conv & Norm
        result = result + self.noise1(noise)
        result = self.adain(result, self.style1(latent_w))
        result = self.lrelu(result)
        result = self.conv2(result)
        result = result + self.noise2(noise)
        result = self.adain(result, self.style2(latent_w))
        result = self.lrelu(result)
        
        return result    

# Very First Convolutional Block
# 5/13: No more downsample, this block is the same sa general ones
# class Early_ConvBlock(nn.Module):
#     '''
#     Used to construct progressive discriminator
#     '''
#     def __init__(self, in_channel, out_channel, size_kernel, padding):
#         super().__init__()
#         self.conv = nn.Sequential(
#             SConv2d(in_channel, out_channel, size_kernel, padding=padding),
#             nn.LeakyReLU(0.2),
#             SConv2d(out_channel, out_channel, size_kernel, padding=padding),
#             nn.LeakyReLU(0.2)
#         )
    
#     def forward(self, image):
#         result = self.conv(image)
#         return result
    
# General Convolutional Block
# 5/13: Downsample is now removed from block module
class ConvBlock(nn.Module):
    '''
    Used to construct progressive discriminator
    '''
    def __init__(self, in_channel, out_channel, size_kernel1, padding1, 
                 size_kernel2 = None, padding2 = None):
        super().__init__()
        
        if size_kernel2 == None:
            size_kernel2 = size_kernel1
        if padding2 == None:
            padding2 = padding1
        
        self.conv = nn.Sequential(
            SConv2d(in_channel, out_channel, size_kernel1, padding=padding1),
            nn.LeakyReLU(0.2),
            SConv2d(out_channel, out_channel, size_kernel2, padding=padding2),
            nn.LeakyReLU(0.2)
        )
    
    def forward(self, image):
        # Downsample now proxyed by discriminator
        # result = nn.functional.interpolate(image, scale_factor=0.5, mode="bilinear", align_corners=False)
        # Conv
        result = self.conv(image)
        return result
        
    
# Main components
class Intermediate_Generator(nn.Module):
    '''
    A mapping consists of multiple fully connected layers.
    Used to map the input to an intermediate latent space W.
    '''
    def __init__(self, n_fc, dim_latent):
        super().__init__()
        layers = [PixelNorm()]
        for i in range(n_fc):
            layers.append(SLinear(dim_latent, dim_latent))
            layers.append(nn.LeakyReLU(0.2))
            
        self.mapping = nn.Sequential(*layers)
    
    def forward(self, latent_z):
        latent_w = self.mapping(latent_z)
        return latent_w    
