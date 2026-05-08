from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('student', '0046_alter_userprofile_phone_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='courseaccessrole',
            name='org',
            field=models.CharField(blank=True, db_index=True, max_length=80),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE edxval_thirdpartytranscriptcredentialsstate MODIFY org varchar(80);",
            reverse_sql="ALTER TABLE edxval_thirdpartytranscriptcredentialsstate MODIFY org varchar(32);",
        ),
    ]
