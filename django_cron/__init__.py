import logging
from datetime import timedelta, datetime
import traceback
import time

from django_cron.models import CronJobLog
from django.conf import settings

try:
    from django.utils import timezone
except ImportError:
    # timezone added in Django 1.4
    import timezone

logger = logging.getLogger('django_cron')

class Schedule(object):
    def __init__(self, run_every_mins=None, run_at_times=[], retry_after_failure_mins=None):
        self.run_every_mins = run_every_mins
        self.run_at_times = run_at_times
        self.retry_after_failure_mins = retry_after_failure_mins


class CronJobBase(object):
    """
    Sub-classes should have the following properties:
    + code - This should be a code specific to the cron being run. Eg. 'general.stats' etc.
    + schedule

    Following functions:
    + do - This is the actual business logic to be run at the given schedule
    """
    pass


class CronJobManager(object):
    """
    A manager instance should be created per cron job to be run.
    Does all the logger tracking etc. for it.
    Used as a context manager via 'with' statement to ensure
    proper logger in cases of job failure.
    """

    def __init__(self, cron_job_class, silent=False, *args, **kwargs):
        super(CronJobManager, self).__init__(*args, **kwargs)

        self.cron_job_class = cron_job_class
        self.silent         = silent

    def should_run_now(self, force=False):
        cron_job = self.cron_job
        """
        Returns a boolean determining whether this cron should run now or not!
        """
        # If we pass --force options, we force cron run
        self.user_time = None
        if force:
            return True
        if cron_job.schedule.run_every_mins != None:

            # We check last job - success or not
            last_job = None
            try:
                last_job = CronJobLog.objects.filter(code=cron_job.code).latest('start_time')
            except CronJobLog.DoesNotExist:
                pass
            if last_job:
                if not last_job.is_success and cron_job.schedule.retry_after_failure_mins:
                    if timezone.now() > last_job.start_time + timedelta(minutes=cron_job.schedule.retry_after_failure_mins):
                        return True
                    else:
                        return False

            previously_ran_successful_cron = None
            try:
                previously_ran_successful_cron = CronJobLog.objects.filter(code=cron_job.code, is_success=True, ran_at_time__isnull=True).latest('start_time')
            except CronJobLog.DoesNotExist:
                pass
            if previously_ran_successful_cron:
                if timezone.now() > previously_ran_successful_cron.start_time + timedelta(minutes=cron_job.schedule.run_every_mins):
                    return True
            else:
                return True
        if cron_job.schedule.run_at_times:
            for time_data in cron_job.schedule.run_at_times:
                user_time = time.strptime(time_data, "%H:%M")
                actual_time = time.strptime("%s:%s" % (datetime.now().hour, datetime.now().minute), "%H:%M")
                if actual_time >= user_time:
                    qset = CronJobLog.objects.filter(code=cron_job.code, start_time__gt=datetime.today().date(), ran_at_time=time_data)
                    if not qset:
                        self.user_time = time_data
                        return True
        return False

    def __enter__(self):
        cron_job = self.cron_job_class
        self.cron_log = CronJobLog(code=cron_job.code, start_time=timezone.now())

        return self

    def __exit__(self, ex_type, ex_value, ex_traceback):
        cron_log = self.cron_log
        cron_msg = self.msg

        if ex_type is not None:
            # some exception occured during job execution
            msg = traceback.format_exception(ex_type, ex_value, ex_traceback)
            msg = "".join(msg)

            if not self.silent:
                if cron_msg:
                    logger.info(cron_msg)
                logger.error(msg)

            if cron_msg:
                offset = 1000 - len(cron_msg) - len(u'\n...\n')
                msg = cron_msg + u'\n...\n' + msg[-offset:]
            else:
                msg = msg[-1000:]

            cron_log.is_success = False
            cron_log.message    = msg
        else:
            cron_log.is_success = True
            cron_log.message    = cron_msg

        cron_log.ran_at_time = getattr(self, 'user_time', None)
        cron_log.end_time = timezone.now()
        try:
            cron_log.save()
        except Exception as e:
            msg = "Error saving cronjob log message: %s" % e
            logger.error(msg)

        return True # prevent exception propagation

    def run(self, force=False):
        """
        apply the logic of the schedule and call do() on the CronJobBase class
        """
        cron_job_class = self.cron_job_class
        if not issubclass(cron_job_class, CronJobBase):
            raise Exception('The cron_job to be run must be a subclass of %s' % CronJobBase.__name__)

        self.cron_job = cron_job_class()

        if self.should_run_now(force):
            logger.debug("Running cron: %s code %s" % (self.cron_job, self.cron_job.code))
            self.msg = self.cron_job.do()
