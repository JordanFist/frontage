from __future__ import absolute_import
from __future__ import print_function

import datetime
import sys

from time import sleep

from .celery import app
from celery.task.control import revoke
from celery import current_task

from scheduler_state import SchedulerState
from utils.red import redis, redis_get
from apps.flags import Flags
from apps.random_flashing import RandomFlashing
from apps.sweep_async import SweepAsync
from apps.sweep_rand import SweepRand

class TestApp():
    def run(self, params):
        print('[TASK] Running a test app. Im doing nothing at all')
        while True:
            pass

def flask_log(msg):
    print(msg, file=sys.stderr)

def clear_all_task():
    app.control.purge()
    if current_task:
        revoke(current_task.request.id, terminate=True)
    sleep(1)
    SchedulerState.set_current_app({})


@app.task
def start_fap(fap_name=None, user_name='Anonymous', params=None):
    SchedulerState.set_app_started_at()
    app_struct = {'name': fap_name, 'username': user_name, 'params': params, 'started_at': datetime.datetime.now().isoformat() }
    SchedulerState.set_current_app(app_struct)
    if fap_name:
        try:
            fap = globals()[fap_name]()
            fap.run(params=params)
        except Exception, e:
            print('Error when starting task'+str(e))
            return 'Error when starting task'+str(e)


@app.task
def start_forced_fap(fap_name=None, user_name='Anonymous', params=None):
    if redis_get(SchedulerState.KEY_FORCED_APP, False) == 'True':
        print('-----------------------')
        print('A forced App is already running')
        print('-----------------------')
        return

    SchedulerState.set_app_started_at()
    app_struct = {'name': fap_name, 'username': user_name, 'params': params, 'started_at': datetime.datetime.now().isoformat() }
    SchedulerState.set_current_app(app_struct)
    if fap_name:
        try:
            fap = globals()[fap_name]()
            redis.set(SchedulerState.KEY_FORCED_APP, True)
            fap.run(params=params)
            return True
        except Exception, e:
            print('Error when starting task '+str(e))
            raise
        finally:
            redis.set(SchedulerState.KEY_FORCED_APP, False)
    return True



