from optparse import make_option

from django.core.management.base import LabelCommand, CommandError

from emailer.models import Email
from utils import locked


class Command(LabelCommand):
    option_list = LabelCommand.option_list + (
        make_option('--force', '-f', dest='force', action='store_true',
                    default=False,
                    help='Send email even to prior recipients.'),
        make_option('--template', '-t', dest='template',
                    help='Template name of email to be sent (required).'),
    )
    help = 'Send an email to the subscribers to a campaign.'
    args = '<campaign campaign ...>'
    label = 'campaign'

    @locked('sendmail')
    def handle_label(self, label, **options):
        """
        Locked command handler to avoid running this command more than once
        simultaneously.
        """
        template = getattr(self, 'template', None)
        if not template:
            template_name = options.get('template', None)
            if not template_name:
                raise CommandError('--template option is required.')
            try:
                template = Email.objects.get(name=template_name)
                self.template = template
            except Email.DoesNotExist:
                raise CommandError(
                    'No email template %s found.' % template_name)

        # Use custom emailer if defined, default otherwise
        emailer_class = getattr(self, 'emailer_class', None)
        if not emailer_class:
            try:
                emailer_class = template.get_emailer_callable()
                self.emailer_class = emailer_class
            except ImportError, e:
                raise CommandError(e)

        emailer = emailer_class(campaign=label, email=template,
                                force=options['force'])
        try:
            emailer.send_email()
        except Exception, e:
            raise CommandError(e)
