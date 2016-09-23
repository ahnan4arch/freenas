from collections import OrderedDict
from datetime import datetime
from gevent.event import Event
from gevent.lock import Semaphore

import enum
import gevent


class State(enum.Enum):
    WAITING = 1
    RUNNING = 2
    SUCCESS = 3
    FAILED = 4


class JobSharedLock(object):
    """
    Shared lock for jobs.
    Each job method can specify a lock which will be shared
    among all calls for that job and only one job can run at a time
    for this lock.
    """

    def __init__(self, queue, name):
        self.queue = queue
        self.name = name
        self.jobs = []
        self.semaphore = Semaphore()

    def add_job(self, job):
        self.jobs.append(job)

    def get_jobs(self):
        return self.jobs

    def remove_job(self, job):
        self.jobs.remove(job)

    def locked(self):
        return self.semaphore.locked()

    def acquire(self):
        return self.semaphore.acquire()

    def release(self):
        return self.semaphore.release()


class JobsQueue(object):

    def __init__(self):
        self.deque = JobsDeque()
        self.queue = []

        # Event responsible for the job queue schedule loop.
        # This event is set and a new job is potentially ready to run
        self.queue_event = Event()

        # Shared lock (JobSharedLock) dict
        self.job_locks = {}

    def all(self):
        return self.deque.all()

    def add(self, job):
        self.deque.add(job)
        self.queue.append(job)
        # A job has been added to the queue, let the queue scheduler run
        self.queue_event.set()

    def get_lock(self, job):
        """
        Get a shared lock for a job
        """
        name = job.get_lock_name()
        if name is None:
            return None

        lock = self.job_locks.get(name)
        if lock is None:
            lock = JobSharedLock(self, name)
            self.job_locks[lock.name] = lock
        lock.add_job(job)
        return lock

    def release_lock(self, job):
        lock = job.get_lock()
        if not lock:
            return
        # Remove job from lock list and release it so another job can use it
        lock.remove_job(job)
        lock.release()

        if len(lock.get_jobs()) == 0:
            self.job_locks.pop(lock.name)

        # Once a lock is released there could be another job in the queue
        # waiting for the same lock
        self.queue_event.set()

    def next(self):
        """
        This is a blocking method.
        Returns when there is a new job ready to run.
        """
        while True:
            # Awaits a new event to look for a job
            self.queue_event.wait()
            found = None
            for job in self.queue:
                lock = self.get_lock(job)
                # Get job in the queue if it has no lock or its not locked
                if lock is None or not lock.locked():
                    found = job
                    job.set_lock(lock)
                    break
            if found:
                # Unlocked job found to run
                self.queue.remove(found)
                # If there are no more jobs in the queue, clear the event
                if len(self.queue) == 0:
                    self.queue_event.clear()
                return found
            else:
                # No jobs available to run, clear the event
                self.queue_event.clear()

    def run(self):
        while True:
            job = self.next()
            gevent.spawn(job.run, self)


class JobsDeque(object):
    """
    A jobs deque to do not keep more than `maxlen` in memory
    with a `id` assigner.
    """

    def __init__(self, maxlen=1000):
        self.maxlen = 1000
        self.count = 0
        self.__dict = OrderedDict()

    def add(self, job):
        self.count += 1
        job.set_id(self.count)
        if len(self.__dict) > self.maxlen:
            self.__dict.popitem(last=False)
        self.__dict[job.id] = job

    def all(self):
        return self.__dict


class Job(object):
    """
    Represents a long running call, methods marked with @job decorator
    """

    def __init__(self, method, args, options):
        self.method = method
        self.args = args
        self.options = options

        self.id = None
        self.lock = None
        self.result = None
        self.state = State.WAITING
        self.progress = {
            'percent': None,
            'description': None,
        }
        self.time_started = datetime.now()
        self.time_finished = None

    def set_id(self, id):
        self.id = id

    def get_lock_name(self):
        lock_name = self.options.get('lock')
        if callable(lock_name):
            lock_name = lock_name(self.args)
        return lock_name

    def get_lock(self):
        return self.lock

    def set_lock(self, lock):
        self.lock = lock
        self.lock.acquire()

    def set_result(self, result):
        self.result = result

    def set_state(self, state):
        if self.state == State.WAITING:
            assert state not in ('WAITING', 'SUCCESS')
        if self.state == State.RUNNING:
            assert state not in ('WAITING', 'RUNNING')
        assert self.state not in (State.SUCCESS, State.FAILED)
        self.state = State.__members__[state]
        self.time_finished = datetime.now()

    def set_progress(self, percent, description=None):
        if percent is not None:
            assert isinstance(percent, int)
            self.progress['percent'] = percent
        if description:
            self.progress['description'] = description

    def run(self, queue):
        """
        Run a Job and set state/result accordingly.
        This method is supposed to run in a greenlet.
        """

        try:
            self.set_state('RUNNING')
            self.set_result(self.method(*([self] + self.args)))
        except:
            self.set_state('FAILED')
            raise
        else:
            self.set_state('SUCCESS')
        finally:
            queue.release_lock(self)

    def __encode__(self):
        return {
            'id': self.id,
            'progress': self.progress,
            'result': self.result,
            'state': self.state.name,
            'time_started': self.time_started,
            'time_finished': self.time_finished,
        }
