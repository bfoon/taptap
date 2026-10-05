from datetime import timedelta
from decimal import Decimal
from unittest import mock
from django.contrib.auth.models import User
from django.test import RequestFactory,TestCase,override_settings
from django.utils import timezone
from .models import Business,Voucher,VoucherDeviceBinding
from .models_member_plans import MemberNotificationSettings,MemberPlan,MemberPlanAssignment
from .models_member_portal import MemberPortalEvent
from . import member_self_service as svc

class MemberSelfServiceTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user("owner@example.com",password="test")
        self.business=Business.objects.create(user=self.user,business_name="Portal Test",owner_name="Owner",
            phone="2200000",trial_ends_at=timezone.now()+timedelta(days=30),is_unlimited=True)
        self.plan=MemberPlan.objects.create(business=self.business,name="Monthly",price=Decimal("1000.00"),
            duration_minutes=43200,duration_unit="months",max_devices=2)
        self.member=Voucher.objects.create(business=self.business,code="jimmy",login_type="member",plan_name="Monthly",
            price=Decimal("1000.00"),duration_minutes=43200,max_devices=2,status="active")
        MemberPlanAssignment.objects.create(member=self.member,plan=self.plan,assigned_by=self.user)
        MemberNotificationSettings.objects.create(member=self.member,email="jimmy@example.com")
        self.binding=VoucherDeviceBinding.objects.create(business=self.business,voucher=self.member,slot_no=1,
            current_mac="AA:BB:CC:DD:EE:FF",label="Jimmy phone")
        self.factory=RequestFactory()

    @mock.patch("core.member_notifications._send",return_value=1)
    def test_magic_link_hashed_and_single_use(self,_send):
        url=svc.issue_magic_link(self.member)
        token=url.rstrip("/").split("/")[-1]
        row=self.member.member_portal_links.first()
        self.assertNotEqual(row.token_hash,token)
        self.assertEqual(svc.consume_magic_link(token).pk,self.member.pk)
        self.assertIsNone(svc.consume_magic_link(token))

    @mock.patch("core.device_lock._forget_mac",return_value=True)
    def test_stop_start_device(self,_forget):
        req=self.factory.post("/"); req.session={}
        svc.device_action(self.member,self.binding.pk,"stop",req)
        self.binding.refresh_from_db(); self.assertTrue(self.binding.member_control.stopped)
        svc.device_action(self.member,self.binding.pk,"start",req)
        self.binding.refresh_from_db(); self.assertFalse(self.binding.member_control.stopped)

    @mock.patch("core.shared_voucher_device_control.remove_one",return_value=(True,"Removed"))
    def test_remove_logged(self,_remove):
        req=self.factory.post("/"); req.session={}
        svc.device_action(self.member,self.binding.pk,"remove",req)
        self.assertTrue(MemberPortalEvent.objects.filter(member=self.member,event="device_remove").exists())

    def test_agreement_required_then_accepted(self):
        self.assertFalse(svc.agreement_ok(self.member))
        req=self.factory.post("/"); req.session={}
        svc.accept_agreement(self.member,req); self.assertTrue(svc.agreement_ok(self.member))

    @override_settings(MEMBER_PAYMENT_CHECKOUT_URL="",MEMBER_PAYMENT_WEBHOOK_SECRET="")
    def test_no_unverified_payment(self):
        req=self.factory.post("/"); req.session={}
        with self.assertRaises(ValueError): svc.create_payment_intent(self.member,req)
