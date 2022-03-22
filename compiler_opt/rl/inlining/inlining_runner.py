# coding=utf-8
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module for collect data of inlining-for-size."""

import io
import os
import tempfile

from typing import Tuple, Dict, Optional

import tensorflow as tf

from compiler_opt.rl import compilation_runner

# TODO(mtrofin): maybe the deadline is a requirement for plugins (such as the
# inliner) and the data collector expects and uses it to define its own? This
# would serve as an extra hint to the developer of a new plugin to make sure
# their long-running tasks have timeouts.
_DEADLINE_IN_SECONDS = 60

_DEFAULT_IDENTIFIER = 'default'


class InliningRunner(compilation_runner.CompilationRunner):
  """Class for collecting data for inlining-for-size.

  Usage:
  inliner = InliningRunner(
                clang_path, llvm_size_path, launcher_path,
                moving_average_decay_rate)
  serialized_sequence_example, default_reward, moving_average_reward,
  policy_reward = inliner.collect_data(
      ir_path, tf_policy_path, default_reward, moving_average_reward)
  """

  def _compile_fn(
      self, file_paths: Tuple[str, str], tf_policy_path: str, reward_only: bool,
      cancellation_manager: Optional[
          compilation_runner.WorkerCancellationManager]
  ) -> Dict[str, Tuple[tf.train.SequenceExample, float]]:
    """Run inlining for the given IR file under the given policy.

    Args:
      file_paths: path to files needed for inlining, Tuple of (.bc, .cmd).
      tf_policy_path: path to TF policy direcoty on local disk.
      reward_only: whether only return native size.
      cancellation_manager: handler for early termination by killing any running
      processes

    Returns:
      A dict mapping from example identifier to tuple containing:
        sequence_example: A tf.SequenceExample proto describing compilation
        trace, None if reward_only == True.
        native_size: Native size of the final native code.

    Raises:
      subprocess.CalledProcessError: if process fails.
      compilation_runner.ProcessKilledError: (which it must pass through) on
      cancelled work.
      RuntimeError: if llvm-size produces unexpected output.
    """
    working_dir = tempfile.mkdtemp()

    log_path = os.path.join(working_dir, 'log')
    output_native_path = os.path.join(working_dir, 'native')

    input_ir_path, cmd_path = file_paths

    sequence_example = tf.train.SequenceExample()
    native_size = 0
    try:
      command_line = []
      if self._launcher_path:
        command_line.append(self._launcher_path)
      command_line.extend([self._clang_path] +
                          compilation_runner.get_command_line_for_bundle(
                              cmd_path, input_ir_path) + [
                                  '-mllvm', '-enable-ml-inliner=development',
                                  '-mllvm', '-training-log=' +
                                  log_path, '-o', output_native_path
                              ])
      if tf_policy_path:
        command_line.extend(
            ['-mllvm', '-ml-inliner-model-under-training=' + tf_policy_path])
      compilation_runner.start_cancellable_process(command_line,
                                                   _DEADLINE_IN_SECONDS,
                                                   cancellation_manager)
      command_line = [self._llvm_size_path, output_native_path]
      output_bytes = compilation_runner.start_cancellable_process(
          command_line,
          timeout=_DEADLINE_IN_SECONDS,
          cancellation_manager=cancellation_manager,
          want_output=True)
      if not output_bytes:
        raise RuntimeError('Empty llvm-size output: %s' %
                           ' '.join(command_line))
      output = output_bytes.decode('utf-8')
      tmp = output.split('\n')
      if len(tmp) != 3:
        raise RuntimeError('Wrong llvm-size output %s' % output)
      tmp = tmp[1].split('\t')
      native_size = int(tmp[0])

      if native_size == 0:
        return {}

      if reward_only:
        return {_DEFAULT_IDENTIFIER: (None, native_size)}

      with io.open(log_path, 'rb') as f:
        sequence_example.ParseFromString(f.read())

      if not sequence_example.HasField('feature_lists'):
        return {}

    finally:
      tf.io.gfile.rmtree(working_dir)

    return {_DEFAULT_IDENTIFIER: (sequence_example, native_size)}