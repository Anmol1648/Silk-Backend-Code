from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('companyprofile', '0004_companyprofile_logo_url'),
    ]

    operations = [
        migrations.CreateModel(
            name='Investor',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('tenant_id', models.UUIDField(db_index=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_deleted', models.BooleanField(db_index=True, default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('source', models.CharField(blank=True, default='', help_text='ai_research | founder | document_extracted | stage0', max_length=32)),
                ('confidence', models.DecimalField(blank=True, decimal_places=2, max_digits=3, null=True)),
                ('source_ref', models.CharField(blank=True, default='', max_length=255)),
                ('verified', models.BooleanField(default=False)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('name', models.CharField(max_length=255)),
                ('investor_type', models.CharField(blank=True, default='Other', max_length=16, choices=[('VC', 'Venture Capital'), ('PE', 'Private Equity'), ('Angel', 'Angel'), ('Strategic', 'Strategic'), ('FamilyOffice', 'Family Office'), ('Other', 'Other')])),
                ('rounds', models.JSONField(blank=True, default=list)),
                ('ownership_pct', models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True)),
                ('holder_category', models.CharField(blank=True, default='Investor', max_length=16)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('deleted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='investors', to='companyprofile.companyprofile')),
                ('verified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'company_investor',
                'ordering': ('sort_order',),
            },
        ),
    ]
