import os
import sys
import time

from six.moves import range
import argparse
import numpy
import posix_ipc
import zmq

from util import mmap
from util import launch_process


class Controller(object):
    """
    Abstract multi-process controller

    This class provides the necessary features to dispatch data mini-batches
    to workers and handle control requests. Using this class should be done
    by having another class inherit from it and override the method
    `handle_control()`.

    .. warning::

        Due to the underlying implementation it is a bad idea to
        attempt to do both in the same process, even on different
        threads.  This will suffer from interlock problems and may
        negate any speedup you could get from using multiple Workers.

        Because of this issue, the class may be split in the future.

    Parameters
    ----------
    port : int
        The port number to communicate over
    control_port : int
        The control port number.
    device_list : list of strings
        Contains device names in clique order (prefer ring topology)
    hwm : int
        High water mark (see pyzmq docs).

    """

    def __init__(self, control_port, port=None, hwm=10, experiment_name="",
                 local_size=0, device_list=list(), multinode=False):

        self._should_stop = False
        self._workers = set()
        self._need_init = True

        self._device_list = device_list
        self._local_size = local_size
        self._count_workers = 0

        # New interface: Multi node
        self._multinode = multinode
        if self._multinode:
            self._init_global_comm()

        # New interface
        if experiment_name:
            logs_folder = os.path.join("PLATOON_LOGS", experiment_name, time.strftime("%Y-%m-%d_%H-%M"))
            os.makedirs(logs_folder)
            for i in range(self._local_size):
                p = launch_process(logs_folder, experiment_name, None, self.device_list[i], "worker")
                self._workers.add(p.pid)

        if port:
            self.init_data(port, hwm)

        self._init_control_socket(control_port)

        # Cleanup and init global lock and job_uid name ##
        self._job_uid = "platoon_{0}_{1}".format(
            os.path.basename(os.path.expanduser('~')), control_port)

        self._lock_name = "{}lock".format(self._job_uid)
        # The ExistentialError is apparently the only way to verify if
        # the semaphore/shared_memory exists.
        try:
            posix_ipc.unlink_semaphore(self._lock_name)
        except posix_ipc.ExistentialError:
            pass
        # Initializing lock
        self._lock = posix_ipc.Semaphore(self._lock_name, posix_ipc.O_CREAT,
                                         initial_value=1)

        self._shmrefs = dict()
        self.shared_buffers = dict()

    def init_data(self, port, hwm=10):
        """
        Initialize the mini-batch socket.

        This must be called before using :meth:`send_mb`.

        Parameters
        ----------
        port : int
            The port to listen on.
        hwm : int
            High water mark, see the pyzmq docs.

        """
        acontext = zmq.Context()
        self.asocket = acontext.socket(zmq.PUSH)
        self.asocket.set_hwm(hwm)
        self.asocket.bind('tcp://*:{}'.format(port))

    def _init_control_socket(self, port):
        """
        Initialize the control socket.

        This must be called before using :meth:`serve`.

        Parameters
        ----------
        port : int
            The port to listen on.

        """
        ccontext = zmq.Context()
        self.csocket = ccontext.socket(zmq.REP)
        self.csocket.bind('tcp://*:{}'.format(port))

    def send_mb(self, arrays):
        """
        Send a mini-batch over the socket.

        This function may block if arrays are being sent faster than
        the clients can handle.

        Parameters
        ----------
        arrays : list of ndarrays
            List of numpy.ndarray to send.  All arrays should be
            contiguous for better performance.

        """
        # The buffer protocol only works on contiguous arrays
        arrays = [numpy.ascontiguousarray(array) for array in arrays]
        headers = [numpy.lib.format.header_data_from_array_1_0(array)
                   for array in arrays]
        self.asocket.send_json(headers, zmq.SNDMORE)
        for array in arrays[:-1]:
            self.asocket.send(array, zmq.SNDMORE)
        self.asocket.send(arrays[-1])

    def handle_control(self, req, worker_id, req_info):
        """
        Re-implement or assign a handler to this function to do
        something with control messages.

        The replacement get one parameter which is the request and
        should return the response which must be a json-encodable
        object. Other code is responsible for handling decoding,
        encoding and the network.

        """
        raise NotImplementedError("The Controller class should not be "
                                  "instantiated directly. Classes that "
                                  "inherit from Controller should override "
                                  "the method `handle_control()`")

    def _handle_base_control(self, req, worker_id, req_info):
        """
        This method handle base control commands.
        Those commands should not be used in the handle_control method.
        All base control commands should start with "platoon-".
        """
        response = None
        if req == "platoon-get_job_uid":
            response = self._job_uid

        elif req == "platoon-get_device":
            response = self._device_list[req_info['local_rank']]

        elif req == "platoon-need_init":
            response = self._need_init
            self._need_init = False

        elif req == "platoon-get_region_info":
            first = self.is_worker_first()  # See :ref:is_worker_first
            if first:
                self._region_id = b"platoon-" + req_info['region_id']
            response = dict()
            response['region_id'] = self._region_id
            response['region_size'] = self._local_size
            response['regional_rank'] = self._device_list.index(req_info['device'])
            response['multinode'] = self._multinode

        elif req == "platoon-init_new_shmem":
            first = self.is_worker_first()  # See :ref:is_worker_first
            if first:
                self._last_shmem_name = "platoon-{0}_{1}_buffer".format(self._job_uid,
                                                                        len(self.shared_buffers))
                try:
                    posix_ipc.unlink_shared_memory(self._last_shmem_name)
                except posix_ipc.ExistentialError:
                    pass

                size = req_info['size']
                self._last_shmref = posix_ipc.SharedMemory(self._last_shmem_name,
                                                           posix_ipc.O_CREAT,
                                                           size=size)
                self._last_shm = mmap(fd=self._last_shmref.fd, length=size)
                self._last_shmref.close_fd()
                # We want every worker to get the same shared memory name that is
                # was declared in the first call of a mass request to this
                # controller for initializing a new shared memory.
                self._shmrefs[self._last_shmem_name] = self._last_shmref
                # Keep for unlinking when closing
                self.shared_buffers[self._last_shmem_name] = self._last_shm
            response = self._last_shmem_name

        elif req == "platoon-am_i_first":
            response = self.is_worker_first()  # See :ref:is_worker_first

        elif req == "platoon-all_reduce":
            dtype = req_info['dtype']
            op = req_info['op']
            array = self.shared_buffers[req_info['shmem']]
            #  mpi_dtype = dtype_to_mpi(dtype)  # TODO
            #  mpi_op = op_to_mpi(op)  # TODO
            self._global_comm.Allreduce([array, mpi_dtype], [array, mpi_dtype],
                                        op=mpi_op)
            # TODO add try/raise/finally and respond with success or failure to
            # worker

        return response

    def is_worker_first(self):
        """Returns True, if in a mass request in a local platoon (workers in a
        single host) a worker's request reaches first its controller

        This will work only if every single worker participates successfully each
        time in a concurrent request of the same type to their controller.
        """
        self._count_workers = (self._count_workers + 1) % self._local_size
        if self._count_workers == 1:
            return True
        return False

    def worker_is_done(self, worker_id):
        self._workers.discard(worker_id)
        self._should_stop = True

    def serve(self):
        """
        This method will handle control messages until the should_stop flag
        has been raised and that all the known worker are done.
        """
        # spin spin spin
        while (not self._should_stop) or self._workers:
            # wait for children, learn which one exited prematurely and why,
            # print error information about dead process and inform that we are
            # aborting, broadcast decision to abort
            # Abort: issue to all the rest of children to clean up and quit,
            # then quit with fail
            query = self.csocket.recv_json()  # Must be non blocking
            #  self._workers.add(query['worker_id'])

            response = self._handle_base_control(query['req'],
                                                 query['worker_id'],
                                                 query['req_info'])
            if response is None:
                response = self.handle_control(query['req'],
                                               query['worker_id'],
                                               query['req_info'])

            self.csocket.send_json(response)
        self.csocket.close()

    def _init_global_comm(self):
        # TODO abort on import failure
        from mpi4py import MPI
        self._global_comm = MPI.COMM_WORLD
        self._global_size = MPI.COMM_WORLD.Get_size()
        self._global_rank = MPI.COMM_WORLD.Get_rank()

    # TODO failure cleanup workers and return


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Base Platoon Controller process. Reigns over a computer node.")
    parser.add_argument('experiment_name', help='The name of your experiment. The launcher will expect to find the files <experiment_name>_worker.py and optionally <experiment_name>_controller.py.')
    single_or_multi = parser.add_mutually_exclusive_group(required=True)
    single_or_multi.add_argument('--single', action='store_true',
                                 help='Indicates that this Controller participates in a single-node platoon.')
    single_or_multi.add_argument('--multi', action='store_false',
                                 help='Indicates that this Controller participates in a multi-node platoon. Requires mpi4py')
    parser.add_argument('-D', '--devices', nargs='+', type=str, metavar='devname',
                        required=False, help='List of Theano device names (e.g. gpu0 or cuda1). Each device will be assigned to a separate worker. If this option is specified, experiment will be run in a single node.')
    parser.add_argument('-nw', '--workers', metavar='num_of_workers',
                        required=True, help='Number of workers spawned by this controller for this host.')

    return parser.parse_args()


def spawn_controller():
    args = parse_arguments()
    if args.single and args.devices is not None:
        devices = args.devices
    else:
        from pygpu import gpuarray as ga
        devcount = ga.get_device_count("cuda", 0)
        devices = ["cuda" + str(i) for i in range(devcount)]
        # TODO search for platoonrc or PLATOON_FLAGS
    if args.workers > len(devices):
        print("\nWARNING! Given {0} workers but {1} given devices. Using {1} workers.".format(args.workers, len(devices)))
        workers = len(devices)
    else:
        workers = args.workers

    controller = Controller(control_port=5567,
                            experiment_name=args.experiment_name,
                            local_size=workers,
                            device_list=devices,
                            multinode=not args.single)
    controller.serve()


if __name__ == '__main__':
    spawn_controller()
