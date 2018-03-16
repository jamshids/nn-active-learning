from matplotlib import pyplot as plt
from scipy.optimize import fmin_ncg
import numpy as np
import linecache
import shutil
import pickle
import scipy
import nrrd
import yaml
import copy
import pdb
import os

import tensorflow as tf
from tensorflow import gradients
from tensorflow.python.ops import math_ops
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops

from skimage.measure import regionprops
from skimage.segmentation import slic

import patch_utils
import PW_NN
import NN



def hessian_vector_product(ys, xs, v):
  """Multiply the Hessian of `ys` wrt `xs` by `v`.
  This is an efficient construction that uses a backprop-like approach
  to compute the product between the Hessian and another vector. The
  Hessian is usually too large to be explicitly computed or even
  represented, but this method allows us to at least multiply by it
  for the same big-O cost as backprop.
  Implicit Hessian-vector products are the main practical, scalable way
  of using second derivatives with neural networks. They allow us to
  do things like construct Krylov subspaces and approximate conjugate
  gradient descent.
  Example: if `y` = 1/2 `x`^T A `x`, then `hessian_vector_product(y,
  x, v)` will return an expression that evaluates to the same values
  as (A + A.T) `v`.
  Args:
    ys: A scalar value, or a tensor or list of tensors to be summed to
        yield a scalar.
    xs: A list of tensors that we should construct the Hessian over.
    v: A list of tensors, with the same shapes as xs, that we want to
       multiply by the Hessian.
  Returns:
    A list of tensors (or if the list would be length 1, a single tensor)
    containing the product between the Hessian and `v`.
  Raises:
    ValueError: `xs` and `v` have different length.
  """ 

  # Validate the input
  length = len(xs)
  if len(v) != length:
    raise ValueError(
        "xs and v must have the same length.")

  # First backprop
  grads = gradients(ys, xs)

  # grads = xs

  assert len(grads) == length

  elemwise_products = [
      math_ops.multiply(grad_elem, 
                        array_ops.stop_gradient(
                            v_elem))
      for grad_elem, v_elem in zip(grads, v) 
      if grad_elem is not None]

  # Second backprop  
  grads_with_none = gradients(
      elemwise_products, xs)
  return_grads = [
      grad_elem if grad_elem is not None \
      else tf.zeros_like(x) \
      for x, grad_elem in zip(xs, grads_with_none)]
  
  return return_grads


def get_hess_vec_product(model):

    v_placeholder = []
    for layer in model.var_dict:
        v_placeholder += [tf.placeholder(
            tf.float32, 
            shape=model.var_dict[layer][0].get_shape(),
            name=model.var_dict[layer][0].name[:-2])]
        v_placeholder += [tf.placeholder(
            tf.float32, 
            shape=model.var_dict[layer][1].get_shape(),
            name=model.var_dict[layer][1].name[:-2])]


    model.v_placeholder = v_placeholder
    hess_vecp = hessian_vector_product(
        model.loss, 
        tf.trainable_variables(),
        model.v_placeholder)

    model.hess_vecp = hess_vecp

def eval_loss_grad_q(model,
                     sess,
                     padded_imgs,
                     mask,
                     test_ind,
                     patch_shape,
                     batch_size,
                     stats):
    """Evaluating gradient of the loss with
    respect to a given test sample
    """
    
    q_patch, q_label = patch_utils.get_patches(
        padded_imgs, [test_ind], 
        patch_shape, True, mask)

    q_hot_label = np.zeros((2,1))
    q_hot_label[0,q_label[0]==0]=1
    q_hot_label[1,q_label[0]==1]=1

    Ltest_grad = sess.run(model.loss_grad, 
                       feed_dict={
                           model.x:q_patch,
                           model.y_:q_hot_label,
                           model.keep_prob:1.})

    return Ltest_grad
    

def get_f_evaluator(model, 
                    sess, 
                    padded_imgs,
                    mask,
                    tr_inds,
                    Lq_grad,
                    patch_shape,
                    batch_size,
                    stats):
    """Providing evaluator of the Hessian-product
    objective:   `1/2 t^T H t - v^T t`
    """

    def eval_fprime(t):

        tensors_list = unravel_vec(model, t)

        x_feed_dict={}
        for i in range(len(tensors_list)):
            x_feed_dict.update({
                model.v_placeholder[i]:
                tensors_list[i]})

        hessp = PW_NN.batch_eval(
            model, sess, 
            padded_imgs, 
            tr_inds,
            patch_shape,
            batch_size,
            stats, 
            'hess_vecp',
            mask,
            x_feed_dict)[0]

        return 0.5*np.dot(t,ravel_tensors(hessp)) - \
            np.dot(ravel_tensors(Lq_grad), t)

    return eval_fprime


def get_fprime_evaluator(model, 
                         sess, 
                         padded_imgs,
                         mask,
                         tr_inds,
                         Lq_grad,
                         patch_shape,
                         batch_size,
                         stats):
    """Providing evaluator of the gradiant
    of the Hessian-vector product objective:
    `H t - v` 
    """

    def eval_fprime(t):

        tensors_list = unravel_vec(model, t)
        x_feed_dict={}
        for i in range(len(tensors_list)):
            x_feed_dict.update({
                model.v_placeholder[i]:
                tensors_list[i]})

        hessp = PW_NN.batch_eval(
            model, sess, 
            padded_imgs, 
            tr_inds,
            patch_shape,
            batch_size,
            stats, 
            'hess_vecp',
            mask,
            x_feed_dict)[0]

        return ravel_tensors(hessp) - \
            ravel_tensors(Lq_grad)

    return eval_fprime
        
def get_hessp_evaluator(model, 
                        sess, 
                        padded_imgs,
                        mask,
                        tr_inds,
                        patch_shape,
                        batch_size,
                        stats):
    """Providing evaluator of the hessian-vector
    product for a given vector:  `H t`
    """

    def eval_hessp(t, vec):

        tensors_list = unravel_vec(model, vec)
        x_feed_dict={}
        for i in range(len(tensors_list)):
            x_feed_dict.update({
                model.v_placeholder[i]:
                tensors_list[i]})

        hessp = PW_NN.batch_eval(
            model, sess, 
            padded_imgs, 
            tr_inds,
            patch_shape,
            batch_size,
            stats, 
            'hess_vecp',
            mask,
            x_feed_dict)[0]

        return ravel_tensors(hessp)

    return eval_hessp


def ravel_tensors(tensors_list):
    """Ravelling (flatenning) a list
    of tensors (values)
    """

    vec = []
    for tensor in tensors_list:
        vec += [np.ravel(tensor)]

    return np.concatenate(vec)

def unravel_vec(model, vec):
    """Un-ravelling a flat vector 
    according to tensor variables in
    a model

    The given vector is assumed to have
    the same number of elements as the
    total number of paramters in the model
    """

    tensor_list = []

    cnt = 0
    for i,layer in enumerate(model.var_dict):
        # weights
        var_shape = model.var_dict[layer][0].shape
        var_shape = [var_shape[i].value for i 
                     in range(len(var_shape))]
        tensor = np.reshape(
            vec[cnt:cnt+np.prod(var_shape)], 
            var_shape)
        tensor_list += [tensor]
        cnt += np.prod(var_shape)

        # biases
        var_shape = model.var_dict[layer][1].shape
        var_shape = [var_shape[i].value for i 
                     in range(len(var_shape))]
        tensor = np.reshape(
            vec[cnt:cnt+np.prod(var_shape)], 
            var_shape)
        tensor_list += [tensor]
        cnt += np.prod(var_shape)

    return tensor_list
        

def sample_influence(model,
                     sess,
                     tr_padded_imgs,
                     tr_mask,
                     tr_inds,
                     tr_stats,
                     q_padded_imgs,
                     q_mask,
                     q_ind,
                     q_stats,
                     patch_shape,
                     batch_size):

    if not(hasattr(model, 'hess_vecp')):
        get_hess_vec_product(model)

    if not(hasattr(model, 'loss_grad')):
        NN.add_loss_grad(model)

    # loss gradient of the (query) sample
    Lq_grad = eval_loss_grad_q(model,
                               sess,
                               q_padded_imgs,
                               q_mask,
                               q_ind,
                               patch_shape,
                               batch_size,
                               q_stats)

    # providing evaluators of the objective,
    # its gradient, and its Hessian-vector
    # multiplier to given to Scipy optimizer
    f = get_f_evaluator(
        model, 
        sess, 
        tr_padded_imgs,
        tr_mask,
        tr_inds,
        Lq_grad,
        patch_shape,
        batch_size,
        tr_stats)
    fprime = get_fprime_evaluator(
        model, 
        sess, 
        tr_padded_imgs,
        tr_mask,
        tr_inds,
        Lq_grad,
        patch_shape,
        batch_size,
        tr_stats)
    hessp = get_hessp_evaluator(
        model, 
        sess, 
        tr_padded_imgs,
        tr_mask,
        tr_inds,
        patch_shape,
        batch_size,
        tr_stats)
    
    soln = fmin_ncg(
        f=f,
        x0=ravel_tensors(Lq_grad),
        fprime=fprime,
        fhess_p=hessp,
        avextol=1e-8,
        maxiter=100)

    return soln
