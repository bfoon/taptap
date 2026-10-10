"""Staff pay: how each team member is paid, and every payment made to them.

A team member has at most one StaffPay (their pay terms):

    fixed   — a monthly salary
    sales   — a share (%) of net sales, optionally on top of a basic pay
    profit  — a share (%) of profit before staff pay, optionally on top of a basic pay

A percentage deal can be limited to one site (router), and can have a guaranteed
minimum and a cap for the month.

Every payment is a StaffPayout. Salary, advance and bonus payments create an
Expense in the "Staff & wages" category, so they flow into Finance (expenses,
profit & loss, charts) without any special case. Deleting that expense voids the
payment as well (CASCADE), so Finance and Payroll can never disagree. A deduction
moves no money and creates no expense — it only lowers what the person is owed.
"""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

PAY_TYPES = [
    ('fixed', 'Fixed monthly salary'),
    ('sales', '% of sales'),
    ('profit', '% of profit'),
]

PAYOUT_KINDS = [
    ('salary', 'Salary'),
    ('advance', 'Advance'),
    ('bonus', 'Bonus'),
    ('deduction', 'Deduction'),
]

# Kinds that move money out of the business (and so become an Expense).
CASH_KINDS = ('salary', 'advance', 'bonus')


class StaffPay(models.Model):
    """The pay terms of one team member in one business."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='staff_pay')
    member = models.OneToOneField('core.TeamMember', on_delete=models.CASCADE, related_name='pay')
    pay_type = models.CharField(max_length=10, choices=PAY_TYPES, default='fixed')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                 help_text='Monthly salary (fixed) or basic pay on top of a percentage deal')
    percent = models.DecimalField(max_digits=7, decimal_places=4, default=0,
                                  help_text='Share of sales or profit, 0–100')
    minimum = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                  help_text='Guaranteed pay for a full month (0 = none)')
    cap = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                              help_text='Most they can earn in a month (0 = no cap)')
    site = models.ForeignKey('core.Router', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                             help_text='Count only this site\'s sales / profit (blank = whole business)')
    method = models.CharField(max_length=20, default='cash', help_text='Usual way they are paid')
    pay_day = models.PositiveSmallIntegerField(default=28, help_text='Day of the month pay is due')
    starts_on = models.DateField(null=True, blank=True, help_text='Pay counts from this date')
    show_basis = models.BooleanField(default=False,
                                     help_text='Let the staff member see the sales / profit figure on My pay')
    active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['member__user__first_name']

    @property
    def is_share(self):
        return self.pay_type in ('sales', 'profit')

    def __str__(self):
        return f'{self.member} — {self.get_pay_type_display()}'


class StaffPayout(models.Model):
    """One payment (or deduction) for a team member, booked against a pay month."""
    business = models.ForeignKey('core.Business', on_delete=models.CASCADE, related_name='staff_payouts')
    member = models.ForeignKey('core.TeamMember', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='payouts')
    member_name = models.CharField(max_length=150, help_text='Kept so history survives removing the member')
    month = models.DateField(db_index=True, help_text='First day of the month this payment is for')
    kind = models.CharField(max_length=10, choices=PAYOUT_KINDS, default='salary')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, default='cash')
    reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)
    calc = models.JSONField(default=dict, blank=True, help_text='How the pay was worked out when this was recorded')
    expense = models.OneToOneField('core.Expense', on_delete=models.CASCADE, null=True, blank=True,
                                   related_name='staff_payout')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_at', '-id']
        indexes = [models.Index(fields=['business', 'month'])]

    @property
    def moves_cash(self):
        return self.kind in CASH_KINDS

    @property
    def method_label(self):
        from .models import PAYMENT_METHODS
        return dict(PAYMENT_METHODS).get(self.method, self.method)

    @property
    def number(self):
        return f'PAY-{timezone.localtime(self.paid_at):%Y%m%d}-{self.pk:06d}'

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount} → {self.member_name} ({self.month:%b %Y})'
