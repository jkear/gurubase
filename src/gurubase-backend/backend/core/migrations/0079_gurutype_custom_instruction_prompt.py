# Generated by Django 4.2.18 on 2025-04-22 10:27

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0078_gurutype_confluence_count_limit_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='gurutype',
            name='custom_instruction_prompt',
            field=models.TextField(blank=True, default='', null=True),
        ),
    ]
