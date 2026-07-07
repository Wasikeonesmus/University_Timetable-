from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0008_roomfeature_course_additional_student_groups_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='sessions_per_week',
            field=models.PositiveIntegerField(
                default=1,
                help_text='How many times per week this course meets (e.g. 2 lectures + 1 lab = separate courses).',
            ),
        ),
    ]
