"""Frontend for exporting Tensorflow graph to ONNX graph

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from onnx_tf.frontend import TensorflowFrontendBase
from onnx import helper


class TensorflowFrontend(TensorflowFrontendBase):
  """ Tensorflow Frontend for ONNX
  """

  ONNX_TO_HANDLER = {
      "reshape": "reshape",
  }

  @classmethod
  def handle_reshape(cls, node, **kwargs):
    return helper.make_node("Reshape", [node.inputs[0], node.inputs[1]],
                            [node.name])
