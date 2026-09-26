from django import forms
from django.contrib.auth.models import User
from .models import Router,VoucherPlan
class RegisterForm(forms.Form):
 business_name=forms.CharField(max_length=180); owner_name=forms.CharField(max_length=180); phone=forms.CharField(max_length=60); email=forms.EmailField(); password=forms.CharField(widget=forms.PasswordInput,min_length=6)
class RouterForm(forms.ModelForm):
 class Meta:
  model=Router; fields=['name','ip_address','api_port','username','password','use_ssl']; widgets={'password':forms.PasswordInput(render_value=True)}
class PlanForm(forms.ModelForm):
 class Meta: model=VoucherPlan; fields=['name','price','duration_hours','max_devices','speed_limit','data_limit_mb','active']
