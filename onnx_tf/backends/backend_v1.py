"""Backend for running ONNX on Tensorflow

To run this, you will need to have Tensorflow installed as well.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from functools import partial
import itertools
import warnings

try:
  from itertools import izip as zip
except ImportError:  # will be 3.x series
  pass

import numpy as np
import tensorflow as tf
from tensorflow.python.ops import array_ops

from onnx_tf.backend import TensorflowBackendBase
from onnx_tf.common import (
    ONNX_OP_TO_TF_OP,
    ONNX_TYPE_TO_TF_TYPE,
)
import onnx.numpy_helper
import onnx.defs


class TensorflowBackend(TensorflowBackendBase):
  """ Tensorflow Backend for ONNX
  """

  @classmethod
  def handle_add(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.add)]

  @classmethod
  def handle_and(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.logical_and)]

  @classmethod
  def handle_arg_max(cls, node, input_dict):
    data = input_dict[node.inputs[0]]
    axis = node.attrs["axis"]
    keepdims = node.attrs.get("keepdims", 1)
    if keepdims == 1:
      warnings.warn("Definition of ArgMax with keepdims enabled is "
                    "incompatible between onnx and tensorflow.", UserWarning)
    return [tf.argmax(data, axis=axis)]

  @classmethod
  def handle_arg_min(cls, node, input_dict):
    data = input_dict[node.inputs[0]]
    axis = node.attrs["axis"]
    keepdims = node.attrs.get("keepdims", 1)
    if keepdims == 1:
      warnings.warn("Definition of ArgMin with keepdims enabled is "
                    "incompatible between onnx and tensorflow.", UserWarning)
    return [tf.argmin(data, axis=axis)]

  @classmethod
  def _compatibility_pool(cls, node, input_dict, pooling_type):

    def _get_pad_shape(auto_pad, input_spatial_shape, kernel_spatial_shape,
                       strides_spatial, output_spatial_shape):
      pad_shape = [0] * len(input_spatial_shape)
      if auto_pad in ("SAME_UPPER", "SAME_LOWER"):
        for i in range(len(input_spatial_shape)):
          pad_shape[i] = (output_spatial_shape[i] - 1) * strides_spatial[i] + kernel_spatial_shape[i] - \
                         input_spatial_shape[i]
      elif auto_pad in ("VALID", ""):
        pass
      return pad_shape

    def _get_output_shape(auto_pad, input_spatial_shape, kernel_spatial_shape,
                          strides_spatial):
      out_shape = [0] * len(input_spatial_shape)
      if auto_pad in ("SAME_UPPER", "SAME_LOWER"):
        for i in range(len(input_spatial_shape)):
          out_shape[i] = int(
              np.ceil(
                  float(input_spatial_shape[i]) / float(strides_spatial[i])))
      elif auto_pad in ("VALID", ""):
        for i in range(len(input_spatial_shape)):
          out_shape[i] = int(
              np.ceil(
                  float(input_spatial_shape[i] - (kernel_spatial_shape[i] - 1))
                  / float(strides_spatial[i])))
      return out_shape

    def py_pool(x, kernel_shape, strides_shape, pads, out_shape, pad_shape,
                pooling_type):
      pooling_type = pooling_type.decode('UTF-8')
      x_shape = np.shape(x)
      spatial_size = len(x_shape[2:])
      pad_attr = [(0, 0), (0, 0)] + [
          (pads[i], pads[i + spatial_size]) for i in range(spatial_size)
      ]
      padded = np.pad(x, pad_attr, mode="constant", constant_values=np.nan)

      y = np.zeros([x_shape[0], x_shape[1]] + list(out_shape))

      for shape in itertools.product(
          range(x_shape[0]), range(x_shape[1]), *[
              range(
                  int((x_shape[i + 2] + pad_shape[i] - kernel_shape[i]
                      ) / strides_shape[i] + 1)) for i in range(spatial_size)
          ]):
        window = padded[shape[0], shape[1]]
        window_vals = np.array([
            window[i] for i in list(
                itertools.product(*[
                    range(strides_shape[i] * shape[i + 2],
                          strides_shape[i] * shape[i + 2] + kernel_shape[i])
                    for i in range(spatial_size)
                ]))
        ])
        if pooling_type == 'AVG':
          f = np.average
        elif pooling_type == 'MAX':
          f = np.max
        else:
          raise NotImplementedError(
              'Pooling type {} does not support. Should be AVG, MAX'.format(
                  pooling_type))
        y[shape] = f(window_vals[np.where(~np.isnan(window_vals))])
      return y.astype(np.float32)

    x = input_dict[node.inputs[0]]
    x_shape = x.shape.as_list()
    spatial_size = len(x_shape[2:])
    kernel_shape = node.attrs["kernel_shape"]
    strides_shape = node.attrs.get("strides", [1] * spatial_size)
    pads = node.attrs.get("pads", [0] * spatial_size * 2)
    auto_pad = node.attrs.get("auto_pad", "")

    # Only auto_pad in ("SAME_LOWER", "") will come here.
    if auto_pad == "SAME_LOWER":
      out_shape = _get_output_shape(auto_pad, x_shape[2:], kernel_shape,
                                    strides_shape)
      pad_shape = _get_pad_shape(auto_pad, x_shape[2:], kernel_shape,
                                 strides_shape, out_shape)
      for i in range(spatial_size):
        pads[i + spatial_size] = pad_shape[i] // 2
        pads[i] = pad_shape[i] - pads[i + spatial_size]
    elif auto_pad == "":
      pad_shape = [
          pads[i] + pads[i + spatial_size] for i in range(spatial_size)
      ]
      out_shape = _get_output_shape(auto_pad, np.add(x_shape[2:], pad_shape),
                                    kernel_shape, strides_shape)

    pooled = tf.py_func(py_pool, [
        x, kernel_shape, strides_shape, pads, out_shape, pad_shape, pooling_type
    ], tf.float32)
    pooled.set_shape(x_shape[0:2] + out_shape)
    return [pooled]

  @classmethod
  def _pool(cls, node, input_dict, pool_func, pooling_type):
    x = input_dict[node.inputs[0]]
    x_rank = len(x.get_shape())

    support_cuda = cls.supports_device("CUDA")
    storage_format, compute_format = cls.get_data_format(x_rank, support_cuda)

    kernel_shape = node.attrs["kernel_shape"]
    strides = node.attrs.get("strides", [1] * (x_rank - 2))

    # By default, do not pad
    pad = None

    if "auto_pad" in node.attrs:
      if node.attrs["auto_pad"] == "SAME_UPPER":
        pad = "SAME"
      elif node.attrs["auto_pad"] == "VALID":
        pad = "VALID"
      elif node.attrs["auto_pad"] == "SAME_LOWER":
        pad = None

    if pad is None:
      return cls._compatibility_pool(node, input_dict, pooling_type)

    if support_cuda:
      pooled = pool_func(
          x,
          kernel_shape,
          padding=pad,
          strides=strides,
          data_format=compute_format)
    else:
      x = tf.transpose(
          x, perm=cls.get_perm_from_formats(storage_format, compute_format))
      pooled = pool_func(
          x,
          kernel_shape,
          padding=pad,
          strides=strides,
          data_format=compute_format)
      pooled = tf.transpose(
          pooled,
          perm=cls.get_perm_from_formats(compute_format, storage_format))

    return [pooled]

  @classmethod
  def handle_average_pool(cls, node, input_dict):
    spatial_dim = list(input_dict[node.inputs[0]].get_shape()[2:])
    kernel_shape = node.attrs.get("kernel_shape", [])
    global_pool = True
    for i in range(len(spatial_dim)):
      global_pool = global_pool and (spatial_dim[i] < kernel_shape[i])

    if global_pool:
      return cls.handle_global_average_pool(node, input_dict)

    # 0 = cannot pad zero
    return cls._pool(node, input_dict, partial(tf.nn.pool, pooling_type='AVG'),
                     'AVG')

  @classmethod
  def handle_batch_normalization(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    total_num_dim = len(x.get_shape())
    scale = cls._explicit_broadcast(input_dict[node.inputs[1]], 1,
                                    total_num_dim)
    bias = cls._explicit_broadcast(input_dict[node.inputs[2]], 1, total_num_dim)
    running_mean = cls._explicit_broadcast(input_dict[node.inputs[3]], 1,
                                           total_num_dim)
    running_variance = cls._explicit_broadcast(input_dict[node.inputs[4]], 1,
                                               total_num_dim)

    variance_epsilon = node.attrs.get("epsilon", 0.00001)
    if node.attrs.get("is_test", 0):
      return [
          tf.nn.batch_normalization(x, running_mean, running_variance, bias,
                                    scale, variance_epsilon)
      ]
    spatial = node.attrs.get("spatial", 1) == 1
    momentum = node.attrs.get("momentum", 0.9)
    axis = [0] if spatial else [0] + list(range(2, total_num_dim))
    mean, variance = tf.nn.moments(x, axis)
    mean = cls._explicit_broadcast(mean, 1, total_num_dim)
    variance = cls._explicit_broadcast(variance, 1, total_num_dim)
    running_mean = running_mean * momentum + mean * (1 - momentum)
    running_variance = running_variance * momentum + variance * (1 - momentum)
    # TODO: need to conform to the documentation here
    return [
        tf.nn.batch_normalization(x, running_mean, running_variance, bias,
                                  scale, variance_epsilon)
    ]

  @classmethod
  def handle_clip(cls, node, input_dict):
    max_val = node.attrs[
        "max"] if "max" in node.attrs.keys() else tf.reduce_max(
            input_dict[node.inputs[0]])
    min_val = node.attrs[
        "min"] if "min" in node.attrs.keys() else tf.reduce_min(
            input_dict[node.inputs[0]])

    return [tf.clip_by_value(input_dict[node.inputs[0]], min_val, max_val)]

  @classmethod
  def handle_concat(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    # apparently this is what's needed for squeezenet to work
    axis = node.attrs.get("axis", 1)
    return [tf.concat(values, axis=axis)]

  @classmethod
  def handle_constant(cls, node, input_dict):
    value = node.attrs["value"]
    elements = onnx.numpy_helper.to_array(value).flatten().tolist()
    dtype = ONNX_TYPE_TO_TF_TYPE[value.data_type]
    return [tf.constant(elements, dtype=dtype, shape=value.dims)]

  @classmethod
  def _conv(cls, node, input_dict, transpose=False):
    x = input_dict[node.inputs[0]]
    x_rank = len(x.get_shape())

    support_cuda = cls.supports_device("CUDA")
    storage_format, compute_format = cls.get_data_format(x_rank, support_cuda)

    in_weights = input_dict[node.inputs[1]]
    weights_rank = len(in_weights.get_shape())
    if transpose:
      # Translate weights from (C x M x KH x KW) to (KH x KW X C X M)
      perm = list(range(2, weights_rank)) + [0, 1]
    else:
      # Translate weights from (M x C x KH x KW) to (KH x KW X C X M)
      perm = list(range(2, weights_rank)) + [1, 0]

    if "kernel_shape" in node.attrs.keys():
      kernel_shape = node.attrs["kernel_shape"]
      assert in_weights.get_shape().as_list()[2:] == kernel_shape, (
          "kernel_shape "
          "attr of convolution does not match the actual weight "
          "passed to this operation, attr {}, actual {}").format(
              kernel_shape,
              in_weights.get_shape().as_list())

    weights = tf.transpose(in_weights, perm)
    dilations = node.attrs.get("dilations", None)
    strides = node.attrs.get("strides", None)

    if "pads" in node.attrs.keys():
      x = cls.get_padding_as_op(x, node.attrs["pads"])

    if "group" in node.attrs:

      weight_groups = tf.split(
          weights, num_or_size_splits=node.attrs["group"], axis=-1)

      if support_cuda:
        xs = tf.split(x, num_or_size_splits=node.attrs["group"], axis=1)
      else:
        x = tf.transpose(
            x, perm=cls.get_perm_from_formats(storage_format, compute_format))
        xs = tf.split(x, num_or_size_splits=node.attrs["group"], axis=-1)

      convolved = [
          tf.nn.convolution(
              x,
              weight,
              "VALID",
              strides=strides,
              dilation_rate=dilations,
              data_format=compute_format)
          for (x, weight) in zip(xs, weight_groups)
      ]

      if len(node.inputs) == 2:
        if support_cuda:
          output = tf.concat(convolved, axis=1)
        else:
          output = tf.concat(convolved, axis=-1)
          output = tf.transpose(
              output,
              perm=cls.get_perm_from_formats(compute_format, storage_format))
      else:
        bias = input_dict[node.inputs[2]]

        if support_cuda:
          output = tf.concat(convolved, axis=1)
          output = tf.nn.bias_add(output, bias, data_format=compute_format)
        else:
          output = tf.concat(convolved, axis=-1)
          output = tf.nn.bias_add(output, bias, data_format=compute_format)
          output = tf.transpose(
              output,
              perm=cls.get_perm_from_formats(compute_format, storage_format))

      return [output]

    if not support_cuda:
      x = tf.transpose(
          x, perm=cls.get_perm_from_formats(storage_format, compute_format))

    convolved = tf.nn.convolution(
        x,
        weights,
        "VALID",
        strides=strides,
        dilation_rate=dilations,
        data_format=compute_format)

    if not support_cuda:
      convolved = tf.transpose(
          convolved,
          perm=cls.get_perm_from_formats(compute_format, storage_format))

    if len(node.inputs) == 2:
      return [convolved]
    else:
      bias = input_dict[node.inputs[2]]
      if not support_cuda:
        convolved = tf.transpose(
            convolved,
            perm=cls.get_perm_from_formats(storage_format, compute_format))
      output = tf.nn.bias_add(convolved, bias, data_format=compute_format)
      if not support_cuda:
        output = tf.transpose(
            output,
            perm=cls.get_perm_from_formats(compute_format, storage_format))
      return [output]

  @classmethod
  def handle_conv(cls, node, input_dict):
    return cls._conv(node, input_dict)

  @classmethod
  def handle_conv_transpose(cls, node, input_dict):
    return cls._conv(node, input_dict, transpose=True)

  @classmethod
  def handle_depth_to_space(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    x_rank = len(x.get_shape())
    support_cuda = cls.supports_device("CUDA")
    storage_format, compute_format = cls.get_data_format(x_rank, support_cuda)
    if support_cuda:
      y = tf.depth_to_space(
          x, block_size=node.attrs["blocksize"], data_format=compute_format)
    else:
      x = tf.transpose(
          x, perm=cls.get_perm_from_formats(storage_format, compute_format))
      y = tf.depth_to_space(
          x, block_size=node.attrs["blocksize"], data_format=compute_format)
      y = tf.transpose(
          y, perm=cls.get_perm_from_formats(compute_format, storage_format))
    return [y]

  @classmethod
  def handle_div(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.divide)]

  @classmethod
  def handle_dropout(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    # Not supported by TF
    is_test = node.attrs["is_test"] if "is_test" in node.attrs.keys() else 0
    if is_test:
      return [x]
    ratio = node.attrs["ratio"] if "ratio" in node.attrs.keys() else 0.5
    return [tf.nn.dropout(x, 1 - ratio)]

  @classmethod
  def handle_elu(cls, node, input_dict):
    x = input_dict[node.inputs[0]]

    alpha = node.attrs.get("alpha", 1.0)
    if "alpha" in node.attrs.keys():
      return [
          tf.cast(x < 0.0, tf.float32) * alpha *
          (tf.exp(x) - 1.0) + tf.cast(x >= 0.0, tf.float32) * x
      ]
    else:
      return [tf.nn.elu(x)]

  @classmethod
  def handle_equal(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.equal)]

  @classmethod
  def handle_flatten(cls, node, input_dict):
    tensor = input_dict[node.inputs[0]]
    axis = node.attrs["axis"] if "axis" in node.attrs.keys() else 1
    shape = tf.shape(tensor)
    split0, split1 = tf.split(shape, [axis, tf.size(shape) - axis])
    split0 = tf.reduce_prod(split0)
    split1 = tf.reduce_prod(split1)
    output_shape = tf.stack([split0, split1])
    return [tf.reshape(tensor, output_shape)]

  @classmethod
  def handle_gemm(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    x = tf.contrib.layers.flatten(x)
    y = input_dict[node.inputs[1]]
    z = input_dict[node.inputs[2]]
    if "transA" in node.attrs.keys() and node.attrs["transA"] == 1:
      x = tf.transpose(x)
    if "transB" in node.attrs.keys() and node.attrs["transB"] == 1:
      y = tf.transpose(y)
    alpha = node.attrs["alpha"] if "alpha" in node.attrs.keys() else 1.0
    beta = node.attrs["beta"] if "beta" in node.attrs.keys() else 1.0
    return [alpha * tf.matmul(x, y) + beta * z]

  @classmethod
  def handle_global_average_pool(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    dims = tf.range(tf.rank(x))
    _, dim_window = tf.split(dims, [2, tf.size(dims) - 2])
    return [tf.reduce_mean(x, axis=dim_window, keep_dims=True)]

  @classmethod
  def handle_global_lp_pool(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    p = node.attrs.get("p", 2.)
    dims = list(range(len(x.shape)))
    dim_window = dims[2:]
    if len(dim_window) > 1 and p == 2:
      p = "euclidean"
    return [tf.norm(x, ord=p, axis=dim_window, keepdims=True)]

  @classmethod
  def handle_global_max_pool(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    dims = tf.range(tf.rank(x))
    _, dim_window = tf.split(dims, [2, tf.size(dims) - 2])
    return [tf.reduce_max(x, axis=dim_window, keep_dims=True)]

  @classmethod
  def handle_hard_sigmoid(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if "alpha" not in node.attrs and "beta" not in node.attrs:
      return [tf.keras.backend.hard_sigmoid(x)]

    alpha = node.attrs["alpha"] if "alpha" in node.attrs else 0.2
    beta = node.attrs["beta"] if "beta" in node.attrs else 0.5
    return [tf.clip_by_value(x * alpha + beta, 0, 1)]

  @classmethod
  def handle_hardmax(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if "axis" in node.attrs and node.attrs["axis"] == len(np.shape(x)) - 1:
      return [tf.contrib.seq2seq.hardmax(x)]

    if "axis" in node.attrs:
      axis = node.attrs["axis"]
      axis = (axis if axis >= 0 else
              len(input_dict[node.inputs[0]].get_shape()) + axis)
    else:
      axis = 1

    shape = tf.shape(x)
    cal_shape = (tf.reduce_prod(shape[0:axis]),
                 tf.reduce_prod(shape[axis:tf.size(shape)]))
    x = tf.reshape(x, cal_shape)

    return [tf.reshape(tf.contrib.seq2seq.hardmax(x), shape)]

  @classmethod
  def handle_less(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.less)]

  @classmethod
  def handle_lp_normalization(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    axis = node.attrs.get("axis", -1)
    p = node.attrs.get("p", 2)
    # https://github.com/onnx/onnx/issues/585
    if isinstance(axis, list):
      axis = [int(v) for v in axis]
    return [tf.norm(x, ord=p, axis=axis, keepdims=True)]

  @classmethod
  def handle_l_r_n(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    alpha = node.attrs["alpha"]
    beta = node.attrs["beta"]
    bias = node.attrs["bias"]
    size = node.attrs["size"]
    tf_alpha = alpha / size
    depth_radius = np.floor([(size - 1) / 2.0])[0]
    # TODO: LRN in tf accepts radius
    # but in ONNX/Caffe accepts diameter.
    # This could be a problem.
    x_t = tf.transpose(x, perm=[0, 2, 3, 1])
    normed = tf.nn.lrn(
        x_t, depth_radius=depth_radius, bias=bias, alpha=tf_alpha, beta=beta)
    normed = tf.transpose(normed, perm=[0, 3, 1, 2])
    return [normed]

  @classmethod
  def handle_l_s_t_m(cls, node, input_dict):
    hidden_size = node.attrs["hidden_size"]
    cell_kwargs = {}

    direction = node.attrs.get("direction", "forward")

    if "clip" in node.attrs:
      cell_kwargs["cell_clip"] = node.attrs["clip"]

    tf_activations = [tf.nn.tanh]
    if "activations" in node.attrs:
      activations = list(map(lambda x: x.lower(), node.attrs["activations"]))
      if activations[0] != "sigmoid" or activations[1] != "tanh":
        warnings.warn(
            "Tensorflow uses sigmiod and tanh as first two activation functions."
            "So activations attr will be set to sigmiod, tanh and {}.".format(
                activations[2]))
      tf_activations = [ONNX_OP_TO_TF_OP[activations[2]]]
      if direction == "bidirectional":
        if activations[3] != "sigmoid" or activations[4] != "tanh":
          warnings.warn(
              "Tensorflow uses sigmiod and tanh as first two activation functions."
              "So activations attr will be set to sigmiod, tanh and {}.".format(
                  activations[4]))
        tf_activations.append(ONNX_OP_TO_TF_OP[activations[5]])

    cell_kwargs["activation"] = tf_activations[0]
    lstm_cell = tf.contrib.rnn.LSTMCell(hidden_size, **cell_kwargs)
    cell = tf.contrib.rnn.MultiRNNCell([lstm_cell])
    if direction == "bidirectional":
      cell_kwargs["activation"] = tf_activations[1]
      lstm_cell_bw = [tf.contrib.rnn.LSTMCell(hidden_size, **cell_kwargs)]
      cell_bw = tf.contrib.rnn.MultiRNNCell([lstm_cell_bw])

    # TODO: handle data types
    if direction == "forward":
      output, state = tf.nn.dynamic_rnn(
          cell, input_dict[node.inputs[0]], time_major=True, dtype=tf.float32)
    elif direction == "bidirectional":
      output, state = tf.nn.bidirectional_dynamic_rnn(
          cell,
          cell_bw,
          input_dict[node.inputs[0]],
          time_major=True,
          dtype=tf.float32)
    elif direction == "reverse":

      def _reverse(input_, seq_dim):
        return array_ops.reverse(input_, axis=[seq_dim])

      time_dim = 0
      inputs_reverse = _reverse(input_dict[node.inputs[0]], time_dim)
      output, state = tf.nn.dynamic_rnn(
          cell, inputs_reverse, time_major=True, dtype=tf.float32)

    state = state[0]
    c, h = state
    states = [h, c]
    outputs = [output]
    outputs.extend(states)
    return outputs

  @classmethod
  def handle_leaky_relu(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if not "alpha" in node.attrs.keys():
      warnings.warn("Provide an alpha value.", UserWarning)
      alpha = 0.01
    else:
      alpha = node.attrs["alpha"]
    tf_op = tf.nn.relu(x) - alpha * tf.nn.relu(-x)
    return [tf_op]

  @classmethod
  def handle_log_softmax(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if "axis" in node.attrs and node.attrs["axis"] == len(np.shape(x)) - 1:
      return [tf.nn.log_softmax(x)]

    if "axis" in node.attrs:
      axis = node.attrs["axis"]
      axis = (axis if axis >= 0 else
              len(input_dict[node.inputs[0]].get_shape()) + axis)
    else:
      axis = 1

    shape = tf.shape(x)
    cal_shape = (tf.reduce_prod(shape[0:axis]),
                 tf.reduce_prod(shape[axis:tf.size(shape)]))
    x = tf.reshape(x, cal_shape)

    return [tf.reshape(tf.nn.log_softmax(x - tf.reduce_max(x)), shape)]

  @classmethod
  def handle_max(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    return [tf.reduce_max(tf.stack(values), axis=0)]

  @classmethod
  def handle_max_pool(cls, node, input_dict):
    return cls._pool(node, input_dict, partial(tf.nn.pool, pooling_type='MAX'),
                     'MAX')

  @classmethod
  def handle_mean(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    return [tf.reduce_mean(tf.stack(values), axis=0)]

  @classmethod
  def handle_min(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    return [tf.reduce_min(tf.stack(values), axis=0)]

  @classmethod
  def handle_mul(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.multiply)]

  @classmethod
  def handle_or(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.logical_or)]

  @classmethod
  def handle_p_relu(cls, node, input_dict):
    """
    Reference implementation at
    https://github.com/tflearn/tflearn/blob/4ba8c8d78bf1bbdfc595bf547bad30580cb4c20b/tflearn/activations.py#L191
    """
    x = input_dict[node.inputs[0]]
    slope = input_dict[node.inputs[1]]
    slope = cls._explicit_broadcast(slope, 1, len(x.get_shape()))
    pos = tf.nn.relu(x)
    neg = slope * (x - abs(x)) * 0.5
    return [pos + neg]

  @classmethod
  def handle_pad(cls, node, input_dict):
    num_dim = int(len(node.attrs["paddings"]) / 2)
    mode = node.attrs["mode"]

    def _compatibility_edge_pad(x, pads):
      x = np.pad(x, pads, mode="edge")
      return x

    value = node.attrs.get("value", 0)
    # tf requires int32 paddings
    pads = tf.constant(
        np.transpose(
            np.array(node.attrs["paddings"]).reshape([2, num_dim]).astype(
                np.int32)))

    x = input_dict[node.inputs[0]]
    if mode.lower() == "edge":
      return [tf.py_func(_compatibility_edge_pad, [x, pads], x.dtype)]

    return [tf.pad(input_dict[node.inputs[0]], pads, mode, None, value)]

  @classmethod
  def handle_pow(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.pow)]

  @classmethod
  def handle_random_normal_like(cls, node, input_dict):
    shape = tf.shape(input_dict[node.inputs[0]])
    mean = node.attrs.get("mean", 0)
    stddev = node.attrs.get("scale", 1)
    dtype = ONNX_TYPE_TO_TF_TYPE[node.attrs["dtype"]]
    seed = node.attrs["seed"] if "seed" in node.attrs.keys() else None
    return [tf.random_normal(shape, mean, stddev, dtype, seed)]

  @classmethod
  def handle_random_uniform_like(cls, node, input_dict):
    shape = tf.shape(input_dict[node.inputs[0]])
    minval = node.attrs.get("low", 0)
    maxval = node.attrs.get("high", 1)
    dtype = ONNX_TYPE_TO_TF_TYPE[node.attrs["dtype"]]
    seed = node.attrs["seed"] if "seed" in node.attrs.keys() else None
    return [tf.random_uniform(shape, minval, maxval, dtype, seed)]

  @classmethod
  def handle_reduce_l1(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    axis = node.attrs["axes"]
    # https://github.com/onnx/onnx/issues/585
    if isinstance(axis, list):
      axis = [int(v) for v in axis]
    keepdims = node.attrs.get("keepdims", 1) == 1
    return [tf.norm(x, ord=1, axis=axis, keepdims=keepdims)]

  @classmethod
  def handle_reduce_sum_square(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    axis = node.attrs["axes"]
    keepdims = node.attrs.get("keepdims", 1) == 1
    return [tf.reduce_sum(tf.square(x), axis=axis, keepdims=keepdims)]

  @classmethod
  def handle_reshape(cls, node, input_dict):
    tensor = input_dict[node.inputs[0]]
    shape = tf.constant(node.attrs["shape"])
    return [tf.reshape(tensor, shape)]

  @classmethod
  def handle_selu(cls, node, input_dict):
    warnings.warn("Definition of Selu is different "
                  "between onnx and tensorflow.", UserWarning)
    if "alpha" not in node.attrs and "gamma" not in node.attrs:
      return [tf.nn.selu(input_dict[node.inputs[0]])]

    x = input_dict[node.inputs[0]]
    alpha = node.attrs["alpha"] if "alpha" in node.attrs else 1.6732
    gamma = node.attrs["gamma"] if "gamma" in node.attrs else 1.0507

    return [
        tf.clip_by_value(x, 0, tf.reduce_max(x)) * gamma +
        (tf.exp(tf.clip_by_value(x, tf.reduce_min(x), 0)) - 1) * alpha * gamma
    ]

  @classmethod
  def handle_slice(cls, node, input_dict):
    x = input_dict[node.inputs[0]]

    full_sizes = x.get_shape().as_list()
    full_begin = [0] * len(full_sizes)

    starts = node.attrs.get("starts")
    ends = node.attrs.get("ends")
    slice_len = len(starts)
    axes = node.attrs.get("axes", list(range(slice_len)))

    for i in range(slice_len):
      ends[i] = full_sizes[axes[i]] + ends[i] if ends[i] < 0 else ends[i]
      ends[i] = np.min([full_sizes[axes[i]], ends[i]])
      starts[i] = np.min([full_sizes[axes[i]], starts[i]])
      full_begin[axes[i]] = starts[i]
      full_sizes[axes[i]] = ends[i] - starts[i]

    return [
        tf.slice(input_dict[node.inputs[0]], tf.constant(full_begin),
                 tf.constant(full_sizes))
    ]

  @classmethod
  def handle_softmax(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if "axis" in node.attrs and node.attrs["axis"] == len(np.shape(x)) - 1:
      return [tf.nn.softmax(x)]

    if "axis" in node.attrs:
      axis = node.attrs["axis"]
      axis = (axis if axis >= 0 else
              len(input_dict[node.inputs[0]].get_shape()) + axis)
    else:
      axis = 1

    shape = tf.shape(x)
    cal_shape = (tf.reduce_prod(shape[0:axis]),
                 tf.reduce_prod(shape[axis:tf.size(shape)]))
    x = tf.reshape(x, cal_shape)

    return [tf.reshape(tf.nn.softmax(x - tf.reduce_max(x)), shape)]

  @classmethod
  def handle_space_to_depth(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    x_rank = len(x.get_shape())
    support_cuda = cls.supports_device("CUDA")
    storage_format, compute_format = cls.get_data_format(x_rank, support_cuda)
    if support_cuda:
      y = tf.space_to_depth(
          x, block_size=node.attrs["blocksize"], data_format=compute_format)
    else:
      x = tf.transpose(
          x, perm=cls.get_perm_from_formats(storage_format, compute_format))
      y = tf.space_to_depth(
          x, block_size=node.attrs["blocksize"], data_format=compute_format)
      y = tf.transpose(
          y, perm=cls.get_perm_from_formats(compute_format, storage_format))
    return [y]

  @classmethod
  def handle_split(cls, node, input_dict):
    split = (tf.constant(node.attrs["split"])
             if "split" in node.attrs else input_dict[node.inputs[1]])
    axis = node.attrs["axis"]
    return list(tf.split(input_dict[node.inputs[0]], split, axis))

  @classmethod
  def handle_sub(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.subtract)]

  @classmethod
  def handle_sum(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    return [tf.reduce_sum(tf.stack(values), axis=0)]

  @classmethod
  def handle_tile(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    axis = input_dict[node.inputs[1]]
    tiles = input_dict[node.inputs[2]]
    multiples = tf.Variable([1] * len(x.shape))
    multiples = multiples[axis].assign(tiles)
    return [tf.tile(x, multiples=multiples)]

  @classmethod
  def handle_thresholded_relu(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    if not "alpha" in node.attrs.keys():
      warnings.warn("Provide an alpha value.", UserWarning)
      alpha = 1
    else:
      alpha = node.attrs["alpha"]

    epsilon = 1e-5
    return [tf.nn.relu(x) - tf.nn.relu(tf.sign(alpha - x + epsilon) * x)]

  @classmethod
  def handle_top_k(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    k = node.attrs["k"] if "k" in node.attrs else 1
    values, indices = tf.nn.top_k(x, k=k)
    return [values, tf.cast(indices, dtype=tf.int64)]

  @classmethod
  def handle_unsqueeze(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    axis_list = sorted(node.attrs["axes"])
    for axis in axis_list:
      x = tf.expand_dims(x, axis=axis)
    return [x]

  @classmethod
  def handle_mat_mul(cls, node, input_dict):
    return [tf.matmul(input_dict[node.inputs[0]], input_dict[node.inputs[1]])]

  @classmethod
  def handle_xor(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.logical_xor)]

  @classmethod
  def handle_greater(cls, node, input_dict):
    return [cls._bin_op(node, input_dict, tf.greater)]
