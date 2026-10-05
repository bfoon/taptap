from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone

class Migration(migrations.Migration):
    dependencies=[("core","0077_member_balance_payment")]
    operations=[
        migrations.CreateModel(name="MemberPortalProfile",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("agreement_version",models.CharField(blank=True,max_length=30)),
            ("agreement_accepted_at",models.DateTimeField(blank=True,null=True)),
            ("agreement_ip",models.GenericIPAddressField(blank=True,null=True)),
            ("auth_version",models.PositiveIntegerField(default=1)),
            ("last_portal_login_at",models.DateTimeField(blank=True,null=True)),
            ("created_at",models.DateTimeField(auto_now_add=True)),("updated_at",models.DateTimeField(auto_now=True)),
            ("member",models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name="member_portal_profile",to="core.voucher"))]),
        migrations.CreateModel(name="MemberPortalMagicLink",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("email",models.EmailField(max_length=254)),("token_hash",models.CharField(db_index=True,max_length=64,unique=True)),
            ("created_at",models.DateTimeField(default=django.utils.timezone.now)),("expires_at",models.DateTimeField(db_index=True)),
            ("used_at",models.DateTimeField(blank=True,null=True)),("request_ip",models.GenericIPAddressField(blank=True,null=True)),
            ("user_agent",models.CharField(blank=True,max_length=255)),
            ("business",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_portal_links",to="core.business")),
            ("member",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_portal_links",to="core.voucher"))],
            options={"ordering":["-created_at","-pk"]}),
        migrations.CreateModel(name="MemberDeviceControl",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("stopped",models.BooleanField(default=False)),("stopped_at",models.DateTimeField(blank=True,null=True)),
            ("updated_at",models.DateTimeField(auto_now=True)),
            ("binding",models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,related_name="member_control",to="core.voucherdevicebinding"))]),
        migrations.CreateModel(name="MemberPortalEvent",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("event",models.CharField(max_length=30)),("detail",models.JSONField(blank=True,default=dict)),
            ("ip_address",models.GenericIPAddressField(blank=True,null=True)),("user_agent",models.CharField(blank=True,max_length=255)),
            ("created_at",models.DateTimeField(db_index=True,default=django.utils.timezone.now)),
            ("business",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_portal_events",to="core.business")),
            ("member",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_portal_events",to="core.voucher"))],
            options={"ordering":["-created_at","-pk"]}),
        migrations.AddIndex(model_name="memberportalevent",index=models.Index(fields=["member","created_at"],name="member_portal_event_member_at")),
        migrations.CreateModel(name="MemberPaymentIntent",fields=[
            ("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),
            ("reference",models.CharField(db_index=True,max_length=80,unique=True)),("kind",models.CharField(max_length=20)),
            ("amount",models.DecimalField(decimal_places=2,max_digits=12)),("currency",models.CharField(default="GMD",max_length=12)),
            ("provider",models.CharField(default="hosted",max_length=40)),("status",models.CharField(default="pending",max_length=20)),
            ("provider_reference",models.CharField(blank=True,max_length=120)),("checkout_url",models.URLField(blank=True,max_length=1000)),
            ("detail",models.JSONField(blank=True,default=dict)),("created_at",models.DateTimeField(db_index=True,default=django.utils.timezone.now)),
            ("paid_at",models.DateTimeField(blank=True,null=True)),("updated_at",models.DateTimeField(auto_now=True)),
            ("business",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_payment_intents",to="core.business")),
            ("member",models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,related_name="member_payment_intents",to="core.voucher"))],
            options={"ordering":["-created_at","-pk"]}),
    ]
