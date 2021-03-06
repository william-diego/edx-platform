"""
Django Celery tasks for service status app
"""

import logging
from smtplib import SMTPException

import requests
import simplejson
from celery import Task, task
from celery.states import FAILURE
from django.conf import settings
from django.core.mail import send_mail

from edxmako.shortcuts import render_to_string
from lms.djangoapps.verify_student.models import SoftwareSecurePhotoVerification
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

ACE_ROUTING_KEY = getattr(settings, 'ACE_ROUTING_KEY', None)
SOFTWARE_SECURE_VERIFICATION_ROUTING_KEY = getattr(settings, 'SOFTWARE_SECURE_VERIFICATION_ROUTING_KEY', None)
log = logging.getLogger(__name__)


class BaseSoftwareSecureTask(Task):
    """
    Base task class for use with Software Secure request.

    Permits updating information about user attempt in correspondence to submitting
    request to software secure.
    """
    abstract = True

    def on_success(self, response, task_id, args, kwargs):
        """
        Update SoftwareSecurePhotoVerification object corresponding to this
        task with info about success.

        Updates user verification attempt to "submitted" if the response was ok otherwise
        set it to "must_retry".
        """
        user_verification_id = kwargs['user_verification_id']
        user_verification = SoftwareSecurePhotoVerification.objects.get(id=user_verification_id)
        if response.ok:
            user_verification.mark_submit()
            log.info(
                'Sent request to Software Secure for user: %r and receipt ID %r.',
                user_verification.user.username,
                user_verification.receipt_id,
            )
            return user_verification

        user_verification.mark_must_retry(response.text)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """
        If max retries have reached and task status is still failing, mark user submission
        with "must_retry" so that it can be retried latter.
        """
        if self.max_retries == self.request.retries and status == FAILURE:
            user_verification_id = kwargs['user_verification_id']
            user_verification = SoftwareSecurePhotoVerification.objects.get(id=user_verification_id)
            user_verification.mark_must_retry()
            log.error(
                'Software Secure submission failed for user %r, setting status to must_retry',
                user_verification.user.username,
                exc_info=True
            )


@task(routing_key=ACE_ROUTING_KEY)
def send_verification_status_email(context):
    """
    Spins a task to send verification status email to the learner
    """
    subject = context.get('subject')
    message = render_to_string(context.get('template'), context.get('email_vars'))
    from_addr = configuration_helpers.get_value(
        'email_from_address',
        settings.DEFAULT_FROM_EMAIL
    )
    dest_addr = context.get('email')

    try:
        send_mail(
            subject,
            message,
            from_addr,
            [dest_addr],
            fail_silently=False
        )
    except SMTPException:
        log.warning(u"Failure in sending verification status e-mail to %s", dest_addr)


@task(
    base=BaseSoftwareSecureTask,
    bind=True,
    default_retry_delay=settings.SOFTWARE_SECURE_REQUEST_RETRY_DELAY,
    max_retries=settings.SOFTWARE_SECURE_RETRY_MAX_ATTEMPTS,
    routing_key=SOFTWARE_SECURE_VERIFICATION_ROUTING_KEY,
)
def send_request_to_ss_for_user(self, user_verification_id, copy_id_photo_from):
    """
    Assembles a submission to Software Secure.

    Keyword Arguments:
        user_verification_id (int) SoftwareSecurePhotoVerification model object identifier.
        copy_id_photo_from (SoftwareSecurePhotoVerification): If provided, re-send the ID photo
                data from this attempt.  This is used for re-verification, in which new face photos
                are sent with previously-submitted ID photos.
    Returns:
        request.Response
    """
    user_verification = SoftwareSecurePhotoVerification.objects.get(id=user_verification_id)
    try:
        headers, body = user_verification.create_request(copy_id_photo_from)
        response = requests.post(
            settings.VERIFY_STUDENT["SOFTWARE_SECURE"]["API_URL"],
            headers=headers,
            data=simplejson.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8'),
            verify=False
        )
        return response
    except Exception as exc:  # pylint: disable=bare-except
        log.error(
            (
                'Retrying sending request to Software Secure for user: %r, Receipt ID: %r '
                'attempt#: %s of %s'
            ),
            user_verification.user.username,
            user_verification.receipt_id,
            self.request.retries,
            settings.SOFTWARE_SECURE_RETRY_MAX_ATTEMPTS,
        )
        log.error(str(exc))
        self.retry()
