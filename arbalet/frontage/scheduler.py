
import json
import datetime
import time

from time import sleep
from utils.red import redis, redis_get
from frontage import Frontage
from tasks.tasks import start_fap, start_default_fap, start_forced_fap, clear_all_task
from collections import OrderedDict
from scheduler_state import SchedulerState

from apps.fap import Fap
from apps.flags import Flags
from apps.random_flashing import RandomFlashing
from apps.sweep_async import SweepAsync
from apps.sweep_rand import SweepRand
from apps.snap import Snap
from apps.snake import Snake
from apps.tetris import Tetris
from utils.sentry_client import SENTRY
from server.flaskutils import print_flush
from db.models import ConfigModel
from db.base import session_factory
from utils.websock import Websock


EXPIRE_SOON_DELAY = 30


class Scheduler(object):

    def __init__(self):
        print_flush('---> Waiting for frontage connection...')
        clear_all_task()

        # Create initial data in DB
        session = session_factory()
        conf = session.query(ConfigModel).first()
        if not conf:
            cm = ConfigModel()
            session.add(cm)
            session.commit()
            session.close()

        self.frontage = Frontage()

        session = session_factory()
        config = session.query(ConfigModel).first()
        if not config:
            conf = ConfigModel()
            session.add(conf)
            session.commit()

        expires = session.query(ConfigModel).all()
        print_flush('--------- DB APP ---')
        for e in expires:
            print_flush(e)
        session.close()
        print_flush('--------------------')

        redis.set(SchedulerState.KEY_USERS_Q, '[]')
        redis.set(SchedulerState.KEY_FORCED_APP, False)

        # SchedulerState.set_current_app({})

        # Dict { Name: ClassName, Start_at: XXX, End_at: XXX, task_id: XXX}
        self.current_app_state = None
        self.queue = None
        # Struct { ClassName : Instance, ClassName: Instance }
        # app.__class__.__name__
        self.apps = OrderedDict([
            (Flags.__name__, Flags()),
            (Tetris.__name__, Tetris()),
            (Snake.__name__, Snake()),
            (Snap.__name__, Snap()),
            (RandomFlashing.__name__, RandomFlashing()),
            (SweepRand.__name__, SweepRand()),
            (SweepAsync.__name__, SweepAsync())
        ])

        SchedulerState.set_registered_apps(self.apps)

    def keep_alive_waiting_app(self):
        queue = SchedulerState.get_user_app_queue()
        for c_app in list(queue):
            # Not alive since last check ?
            if time.time() > (
                    c_app['last_alive'] +
                    SchedulerState.DEFAULT_KEEP_ALIVE_DELAY):
                # to_remove.append(i)
                queue.remove(c_app)

    def keep_alive_current_app(self, current_app):
        if not current_app.get('is_default', False) and not current_app.get('is_forced', False) and \
                        time.time() > (current_app['last_alive'] + SchedulerState.DEFAULT_CURRENT_APP_KEEP_ALIVE_DELAY):
            self.stop_app(current_app, Fap.CODE_EXPIRE, 'User has disappeared')
            return True
        return False

    def check_on_off_table(self):
        now = datetime.datetime.now()
        sunrise = SchedulerState.get_scheduled_off_time()
        sunset = SchedulerState.get_scheduled_on_time()

        if sunset < now and now < sunrise:
            SchedulerState.set_frontage_on(True)
        else:
            SchedulerState.set_frontage_on(False)

    def disable_frontage(self):
        SchedulerState.clear_user_app_queue()
        self.stop_app(SchedulerState.get_current_app(),
                                Fap.CODE_CLOSE_APP,
                                'The admin started a forced app')

    def stop_app(self, c_app, stop_code=None, stop_message=None):
        # flask_log(" ========= STOP_APP ====================")
        if not c_app or 'task_id' not in c_app:
            return

        from tasks.celery import app
        if not c_app.get('is_default', False) and not c_app.get('is_forced', False):
            if stop_code and stop_message and 'username' in c_app:
                Websock.send_data(stop_code, stop_message, c_app['username'])

        sleep(0.1)
        # revoke(c_app['task_id'], terminate=True, signal='SIGUSR1')
        # app.control.revoke(c_app['task_id'], terminate=True, signal='SIGUSR1')
        app.control.revoke(c_app['task_id'], terminate=True)
        self.frontage.fade_out()

        sleep(0.05)

    def run_scheduler(self):
        # check usable value, based on ON/OFF AND if a forced app is running
        SchedulerState.set_usable((not SchedulerState.get_forced_app()) and SchedulerState.is_frontage_on())
        enable_state = SchedulerState.get_enable_state()
        if enable_state == 'scheduled':
            self.check_on_off_table()
        elif enable_state == 'on':
            SchedulerState.set_frontage_on(True)
        elif enable_state == 'off':
            SchedulerState.set_frontage_on(False)

        if SchedulerState.is_frontage_on():
            self.check_app_scheduler()
        else:
            # improvement : add check to avoid erase in each loop
            self.disable_frontage()
            self.frontage.erase_all()

    def stop_current_app_start_next(self, queue, c_app, next_app):
        SchedulerState.set_event_lock(True)
        print_flush('===> REVOKING APP')
        self.stop_app(c_app, Fap.CODE_EXPIRE, 'Someone else turn')
        # Start app
        print_flush("## Starting {} [stop_current_app_start_next]".format(next_app['name']))
        start_fap.apply_async(args=[next_app], queue='userapp')
        SchedulerState.wait_task_to_start()

    def app_is_expired(self, c_app):
        now = datetime.datetime.now()
        return now > datetime.datetime.strptime(c_app['expire_at'], "%Y-%m-%d %H:%M:%S.%f")

    def start_default_app(self):
        default_scheduled_app = SchedulerState.get_next_default_app()
        if default_scheduled_app:
            # if not default_scheduled_app['expires'] or default_scheduled_app['expires'] == 0: # TODO restore when each default app has a duration
            #    default_scheduled_app['expires'] = SchedulerState.get_default_fap_lifetime()
            default_scheduled_app['expires'] = SchedulerState.get_default_fap_lifetime()
            default_scheduled_app['default_params']['name'] = default_scheduled_app['name']  # Fix for Colors (see TODO refactor in colors.py)
            SchedulerState.set_event_lock(True)

            print_flush("## Starting {} [DEFAULT]".format(default_scheduled_app['name']))
            start_default_fap.apply_async(args=[default_scheduled_app], queue='userapp')
            SchedulerState.wait_task_to_start()

    def check_app_scheduler(self):
        # check keep alive app (in user waiting app Q)
        self.keep_alive_waiting_app()

        # collect usefull struct & data
        queue = SchedulerState.get_user_app_queue()  # User waiting app
        c_app = SchedulerState.get_current_app()  # Current running app
        now = datetime.datetime.now()

        forced_app = SchedulerState.get_forced_app_request()

        # Is a app running ?
        if c_app:
            if SchedulerState.get_close_app_request():
                self.stop_app(c_app, None, 'Executing requested app closure')
                redis.set(SchedulerState.KEY_CURRENT_RUNNING_APP, '{}')
                redis.set(SchedulerState.KEY_STOP_APP_REQUEST, 'False')
                return
            if len(forced_app) > 0 and not SchedulerState.get_forced_app():
                SchedulerState.clear_user_app_queue()
                self.stop_app(c_app, Fap.CODE_CLOSE_APP, 'The admin started a forced app')
                return
            # do we kill an old app no used ? ?
            if self.keep_alive_current_app(c_app):
                return
            # is expire soon ?
            if not c_app.get('is_default', False) and now > (datetime.datetime.strptime(c_app['expire_at'], "%Y-%m-%d %H:%M:%S.%f") - datetime.timedelta(seconds=EXPIRE_SOON_DELAY)):
                if not SchedulerState.get_expire_soon():
                    Fap.send_expires_soon(EXPIRE_SOON_DELAY, c_app['username'])
            # is the current_app expired ?
            if self.app_is_expired(c_app) or c_app.get('is_default', False):
                # is the current_app a FORCED_APP ?
                if SchedulerState.get_forced_app():
                    self.stop_app(c_app)
                    return
                # is some user-app are waiting in queue ?
                if len(queue) > 0:
                    next_app = queue[0]
                    self.stop_current_app_start_next(queue, c_app, next_app)
                    return
                else:
                    # is a defautl scheduled app ?
                    if c_app.get('is_default', False) and self.app_is_expired(c_app):
                        print_flush('===> Stoping Default Scheduled app')
                        self.stop_app(c_app)
                        return
                    # it's a USER_APP, we let it running, do nothing
                    else:
                        # is a defautl scheduled app ?
                        if c_app.get('is_default', False) and self.app_is_expired(c_app):
                            print_flush('===> Stoping Default Scheduled app')
                            self.stop_app(c_app)
                            return
                        # it's a USER_APP, we let it running, do nothing
                        else:
                            pass
        else:
            if len(forced_app) > 0 and not SchedulerState.get_forced_app():
                print_flush("## Starting {} [FORCED]".format(forced_app['name']))
                SchedulerState.set_event_lock(True)
                SchedulerState.clear_forced_app_request()
                start_forced_fap.apply_async(args=[forced_app], queue='userapp')
                redis.set(SchedulerState.KEY_FORCED_APP, 'True')
                return
            # is an user-app waiting in queue to be started ?
            elif len(queue) > 0:
                SchedulerState.set_event_lock(True)
                start_fap.apply_async(args=[queue[0]], queue='userapp')
                print_flush(" Starting {} [QUEUE]".format(queue[0]['name']))
                return
            else:
                return self.start_default_app()

    def print_scheduler_info(self):
        if self.count % 10 == 0:
            print_flush(" ========== Scheduling ==========")
            print_flush("-------- Enable State")
            print_flush(SchedulerState.get_enable_state())
            print_flush("-------- Is Frontage Up?")
            print_flush(SchedulerState.is_frontage_on())
            print_flush("-------- Usable?")
            print_flush(SchedulerState.usable())
            print_flush("-------- Current App")
            print_flush(SchedulerState.get_current_app())
            print_flush('Forced App ?', SchedulerState.get_forced_app())
            print_flush("---------- Waiting Queue")
            print_flush(SchedulerState.get_user_app_queue())
            if SchedulerState.get_enable_state() == 'scheduled':
                print_flush("---------- Scheduled ON")
                print_flush(SchedulerState.get_scheduled_on_time())
                print_flush("---------- Scheduled OFF")
                print_flush(SchedulerState.get_scheduled_off_time())
        self.count += 1

    def run(self):
        # last_state = False
        # we reset the value
        SchedulerState.set_frontage_on(True)
        SchedulerState.set_enable_state(SchedulerState.get_enable_state())
        # usable = SchedulerState.usable()
        print_flush('[SCHEDULER] Entering loop')
        self.frontage.start()
        self.count = 0
        while True:
            if SchedulerState.is_event_lock():
                print_flush('Locked')
                sleep(0.1)
            else:
                self.run_scheduler()
                self.print_scheduler_info()
                sleep(0.1)


def load_day_table(file_name):
    with open(file_name, 'r') as f:
        SUN_TABLE = json.loads(f.read())
        redis.set(SchedulerState.KEY_DAY_TABLE, json.dumps(SUN_TABLE))

if __name__ == '__main__':
    try:
        load_day_table(SchedulerState.CITY)
        scheduler = Scheduler()
        scheduler.run()
    except Exception as e:
        SENTRY.captureException()
        print(repr(e))
