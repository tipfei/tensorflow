# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
# ===================================================================

"""Tpu Estimator class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading
from six.moves import queue as Queue  # pylint: disable=redefined-builtin

from tensorflow.contrib.tpu.python.tpu import tpu
from tensorflow.contrib.tpu.python.tpu import tpu_config
from tensorflow.contrib.tpu.python.tpu import tpu_feed
from tensorflow.contrib.tpu.python.tpu import training_loop

from tensorflow.python.estimator import estimator as estimator_lib
from tensorflow.python.estimator import model_fn as model_fn_lib
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training import session_run_hook
from tensorflow.python.training import training


def _tpu_job(run_config):
  # The tpu job is determined by the run_config. Right now, this method is
  # required as tpu_config is not part of the RunConfig.
  return None if run_config.master in ['', 'local'] else 'tpu_worker'


class _SIGNAL(object):
  """Signal used to control the input thread of infeed."""
  NEXT_BATCH = 1
  STOP = 2


class InfeedThreadController(object):
  """This wraps the infeed thread and stops when Estimator train finishes.

  For model_fn wrapper, it is not possible to know when the `train` API will
  stop. It could be the cases that the `max_steps` is reached or some hook
  requests the stop in the monitored_session.

  This controller (with coordination with `TpuInfeedSessionHook`) does the
  following:

  1) It pre-infeeds one `batch` data for current TPU iterations.

  2) When `before_run` of `TpuInfeedSessionHook` is called, one more `batch`
  data will be infed.

  3) When `end` of `TpuInfeedSessionHook` is called, the thread will end
  gracefully.

  So, we might need to adjust the algorithrm here if the IO is slower than the
  computation.
  """

  def __init__(self, session, enqueue_ops, iterations):
    self._signal_queue = Queue.Queue()
    self._input_thd = threading.Thread(target=self._input_thread_fn_for_loading,
                                       args=(session, enqueue_ops, iterations))
    self._input_thd.daemon = True
    self._input_thd.start()

  def _input_thread_fn_for_loading(self, session, enqueue_ops, iterations):
    count = 0
    while True:
      signal = self._signal_queue.get()
      if signal == _SIGNAL.STOP:
        logging.info('Stop Infeed input thread.')
        return

      for i in range(iterations):
        logging.debug('InfeedEnqueue data for iteration (%d, %d)', count, i)
        session.run(enqueue_ops)
      count += 1

  def load_next_batch(self):
    self._signal_queue.put(_SIGNAL.NEXT_BATCH)

  def join(self):
    logging.info('Waiting for InputThread to exit.')
    self._signal_queue.put(_SIGNAL.STOP)
    self._input_thd.join()


class TpuInfeedSessionHook(session_run_hook.SessionRunHook):
  """A Session hook setting up the TPU initialization and infeed.

  This hook does two major things:
  1. initialize and shutdown TPU system (maybe a separated hook)
  2. launch and join the input thread for infeed.
  """

  def __init__(self, run_config, enqueue_fn):
    self._iterations = run_config.tpu_config.iterations_per_loop
    self._enqueue_fn = enqueue_fn
    self._tpu_job = _tpu_job(run_config)

  def begin(self):
    self._enqueue_ops = self._enqueue_fn()
    logging.info('TPU job name %s', self._tpu_job)
    self._init_op = [tpu.initialize_system(job=self._tpu_job)]
    self._finalize_op = [tpu.shutdown_system(job=self._tpu_job)]

  def after_create_session(self, session, coord):
    logging.info('Init TPU system')
    session.run(self._init_op)

    logging.info('Start infeed input thread controller')
    self._infeed_thd_controller = InfeedThreadController(
        session, self._enqueue_ops, self._iterations)

  def before_run(self, run_context):
    logging.info('Load next batch of data to infeed.')
    self._infeed_thd_controller.load_next_batch()

  def end(self, session):
    logging.info('Stop infeed input thread controller')
    self._infeed_thd_controller.join()

    logging.info('Shutdown TPU system.')
    session.run(self._finalize_op)


class TpuEstimator(estimator_lib.Estimator):
  """Estimator with TPU support.

  The only difference is a wrapped  model_fn is set in the constructor.
  """

  def __init__(self,
               model_fn=None,
               model_dir=None,
               config=None,
               params=None,
               use_tpu=True):
    if use_tpu:
      model_function = wrapped_model_fn(model_fn, config)
    else:
      model_function = model_fn

    super(TpuEstimator, self).__init__(
        model_fn=model_function,
        model_dir=model_dir,
        config=config,
        params=params)
    if not isinstance(config, tpu_config.RunConfig):
      raise ValueError('`config` must be `tpu_config.RunConfig`')

  def _create_global_step(self, graph):
    """Creates a global step suitable for TPUs.

    Args:
      graph: The graph in which to create the global step.

    Returns:
      A global step `Tensor`.

    Raises:
      ValueError: if the global step tensor is already defined.
    """
    graph = graph or ops.get_default_graph()
    if training.get_global_step(graph) is not None:
      raise ValueError('"global_step" already exists.')
    # Create in proper graph and base name_scope.
    with graph.as_default() as g, g.name_scope(None):
      return variable_scope.get_variable(
          ops.GraphKeys.GLOBAL_STEP,
          shape=[],
          dtype=dtypes.int32,
          initializer=init_ops.zeros_initializer(),
          trainable=False,
          use_resource=True,
          collections=[ops.GraphKeys.GLOBAL_VARIABLES,
                       ops.GraphKeys.GLOBAL_STEP])


# TODO(xiejw): Improve the structure of this input_fn to infeed converion.
# The code now looks not like Estimator style. We need to abstract many
# details.
def _create_infeed_enqueue_ops_and_dequeue_fn(run_config, features, labels):
  """Utility to convert input_fn to enqueue and dequeue fns for TPU.

  Mainly, three things need to be done here.
  1. Calls the input_fn many times (`num_shards`) to infeed the data into TPU
  2. Create a dequeue_fn used by the train_step inside TPU execution to
  dequeue the tensors.
  3. Sets up the input thread to infeed.

  Args:
    run_config: run_config
    features: features
    labels: labels

  Returns:
    A tuple of (dequeue_fn, and thread main function)
  """
  infeed_names = None
  infeed_tuple = []
  if isinstance(features, dict):
    # We need a fixed ordering for enqueueing and dequeueing.
    infeed_names = [name for name in features]
    infeed_tuple.extend([features[name] for name in infeed_names])
  else:
    infeed_tuple.append(features)
  # TODO(jhseu): Handle multi-head and None labels
  infeed_tuple.append(labels)
  # TODO(jhseu): Update when b/36470756 is settled.
  infeed_queue = tpu_feed.InfeedQueue(
      tuple_types=[t.dtype for t in infeed_tuple],
      tuple_shapes=[t.shape for t in infeed_tuple])
  infeed_queue.set_number_of_shards(run_config.tpu_config.num_shards)

  def dequeue_fn():
    """dequeue_fn is used by the train_step in TPU to retrieve the tensors."""
    values = infeed_queue.generate_dequeue_op()
    if infeed_names is None:
      return values
    # Restore the feature dictionary and label.
    dequeued_features = {}
    for i in range(len(values) - 1):
      dequeued_features[infeed_names[i]] = values[i]
    label = values[-1]
    return dequeued_features, label

  def enqueue_fn():
    """enqueue_fn is used to add ops to the graph to send tensors."""
    job = _tpu_job(run_config)
    def placement_function(index):
      if job is None:
        return '/replica:0/task:0/device:CPU:0'
      else:
        return '/job:%s/replica:0/task:%d/device:CPU:0' % (job, index / 8)
    return infeed_queue.split_inputs_and_generate_enqueue_ops(
        infeed_tuple, placement_function=placement_function)

  return (dequeue_fn, enqueue_fn)


def wrapped_model_fn(model_fn, run_config):
  """Returns a new model_fn, which wraps the TPU support."""

  # Verifies the model_fn signature according to Estimator framework.
  estimator_lib._verify_model_fn_args(model_fn, params=None)  # pylint: disable=protected-access

  def _model_fn(features, labels, mode):
    """model_fn."""
    # TODO(jhseu): Move to EVAL and PREDICT to TPU.
    if mode != model_fn_lib.ModeKeys.TRAIN:
      return model_fn(features, labels, mode)

    dequeue_fn, enqueue_fn = (
        _create_infeed_enqueue_ops_and_dequeue_fn(run_config, features, labels))

    loss = _train_on_tpu_shards(
        run_config,
        train_step=_convert_model_fn_to_train_step(
            model_fn, dequeue_fn, mode, run_config))

    # Gets the variables back from TPU nodes. This means the variables updated
    # by TPU will now be *synced* to host memory.
    update_ops = [
        array_ops.check_numerics(v.read_value(),
                                 'Gradient for %s is NaN' % v.name).op
        for v in variables.trainable_variables()
    ]

    hooks = [
        TpuInfeedSessionHook(run_config, enqueue_fn),
        training.LoggingTensorHook(
            {'loss': array_ops.identity(loss),
             'step': training.get_global_step()},
            every_n_secs=30)
    ]

    return model_fn_lib.EstimatorSpec(
        mode,
        loss=array_ops.identity(loss),
        training_hooks=hooks,
        train_op=control_flow_ops.group(*update_ops))
  return _model_fn


def _convert_model_fn_to_train_step(model_fn, dequeue_fn, mode, run_config):
  """generates a train step based on the model_fn."""

  def _call_model_fn(features, labels):
    """Calls the model_fn with required parameters."""
    model_fn_args = estimator_lib._model_fn_args(model_fn)  # pylint: disable=protected-access
    kwargs = {}
    if 'mode' in model_fn_args:
      kwargs['mode'] = mode
    # Uncomment the following lines once `params` is supported.
    #   if 'params' in model_fn_args:
    #     kwargs['params'] = params
    if 'config' in model_fn_args:
      kwargs['config'] = run_config
    return model_fn(features=features, labels=labels, **kwargs)

  def _verify_estimator_spec(estimator_spec):
    """Validates the estimator_spec."""
    err_msg = '{} returned by EstimatorSpec is not supported in TPUEstimator.'
    if estimator_spec.training_chief_hooks:
      raise ValueError(err_msg.format('training_chief_hooks'))
    if estimator_spec.training_hooks:
      raise ValueError(err_msg.format('training_hooks'))
    return estimator_spec

  def train_step(loss):
    """Training step function for use inside a while loop."""
    del loss  # unused; required in function signature.
    features, labels = dequeue_fn()

    # TODO(xiejw): how to do we support hook and savers in the original
    # model_fn. Realistically, the original
    # model_fn will be excuted on TPU chips in a replica way. The hooks
    # returned by the model_fn cannot be supported at all. If we have to,
    # the graph construction part in the model_fn should be separated from the
    # control part (such as hooks and savers). By that the graph construction
    # could de defered on TPU chip, while the control logic can stay in host.
    estimator_spec = _verify_estimator_spec(_call_model_fn(features, labels))
    loss, train_op = estimator_spec.loss, estimator_spec.train_op
    with ops.control_dependencies([train_op]):
      return array_ops.identity(loss)
  return train_step


def _train_on_tpu_shards(run_config, train_step):
  """Executes the `train_step` on all shards."""
  def train_shard():
    return training_loop.repeat(run_config.tpu_config.iterations_per_loop,
                                train_step,
                                [1e7],  # initial_loss
                                name='loop')

  (loss,) = tpu.shard(train_shard,
                      inputs=[],
                      num_shards=run_config.tpu_config.num_shards,
                      outputs_from_all_shards=False)
  return loss
