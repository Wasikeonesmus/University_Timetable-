# Generated migration: fix BUG 3 — add 'PENDING' to GenerationLog.STATUS_CHOICES
# The GenerationLog model used 'PENDING' as a sentinel status throughout signals.py,
# views.py, and scheduling_service.py, but it was never declared in STATUS_CHOICES.
# This caused it to be rejected by form validation and display as a raw string in the admin.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0023_alter_constraint_constraint_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='generationlog',
            name='status',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('PENDING',    'Pending'),    # FIX BUG 3: Added — used as sentinel for background tasks
                    ('OPTIMAL',    'Optimal'),
                    ('FEASIBLE',   'Feasible'),
                    ('INFEASIBLE', 'Infeasible'),
                    ('ERROR',      'Error'),
                ],
            ),
        ),
    ]
