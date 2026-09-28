from django import forms
from django.contrib.auth.models import User
from .models import Router,VoucherPlan
class RegisterForm(forms.Form):
 business_name=forms.CharField(max_length=180,widget=forms.TextInput(attrs={'class':'form-control','placeholder':'e.g. Kairaba Wi-Fi','autocomplete':'organization'}))
 owner_name=forms.CharField(max_length=180,widget=forms.TextInput(attrs={'class':'form-control','placeholder':'First and last name','autocomplete':'name'}))
 phone=forms.CharField(max_length=60,widget=forms.TextInput(attrs={'class':'form-control','placeholder':'+220 700 0000','autocomplete':'tel','inputmode':'tel'}))
 email=forms.EmailField(widget=forms.EmailInput(attrs={'class':'form-control','placeholder':'you@yourbusiness.com','autocomplete':'email'}))
 password=forms.CharField(min_length=8,widget=forms.PasswordInput(attrs={'class':'form-control','placeholder':'At least 8 characters','autocomplete':'new-password'}))
 def clean_email(self):
  from django.contrib.auth.models import User as U
  from .auth_security import validate_email_address
  email,err=validate_email_address(self.cleaned_data['email'])
  if err: raise forms.ValidationError(err)
  if U.objects.filter(username=email).exists(): raise forms.ValidationError('An account with this email already exists. Sign in instead.')
  return email
 def clean_phone(self):
  import re
  v=self.cleaned_data['phone'].strip()
  if len(re.sub(r'\D','',v))<7: raise forms.ValidationError('Enter a phone number customers and our team can reach you on.')
  return v
 def clean(self):
  data=super().clean()
  pw=data.get('password')
  if pw:
   from django.contrib.auth.password_validation import validate_password
   from django.contrib.auth.models import User as U
   try: validate_password(pw,U(username=data.get('email',''),email=data.get('email',''),first_name=data.get('owner_name','')))
   except forms.ValidationError as e: self.add_error('password',e)
  return data
class RouterForm(forms.ModelForm):
 class Meta:
  model=Router; fields=['name','ip_address','api_port','username','password','use_ssl']; widgets={'password':forms.PasswordInput(render_value=True)}
class PlanForm(forms.ModelForm):
 duration_value=forms.IntegerField(min_value=1,initial=1,label='Duration',required=False)
 duration_unit=forms.ChoiceField(choices=[('minutes','Minutes'),('hours','Hours'),('days','Days'),('months','Months (30 days)'),('unlimited','Unlimited (no time limit)')],initial='days',label='Unit')
 field_order=['name','is_free','price','duration_value','duration_unit','max_devices','speed_limit','data_limit_mb','active']
 class Meta:
  model=VoucherPlan; fields=['name','is_free','price','max_devices','speed_limit','data_limit_mb','active']
  labels={'is_free':'Free plan (no price)'}
 def __init__(self,*a,**kw):
  super().__init__(*a,**kw)
  if self.instance and self.instance.pk:
   from .durations import split
   v,u=split(self.instance.duration_minutes,self.instance.duration_unit)
   self.fields['duration_value'].initial=v;self.fields['duration_unit'].initial=u
  self.fields['price'].required=False
 def clean(self):
  data=super().clean()
  from .durations import to_minutes
  if data.get('duration_unit')!='unlimited' and not data.get('duration_value'):
   self.add_error('duration_value','Enter how long the plan lasts, or choose Unlimited.')
  else:
   try: data['duration_minutes']=to_minutes(data.get('duration_value'),data.get('duration_unit'))
   except ValueError as e: self.add_error('duration_value',str(e))
  if data.get('is_free') or data.get('price') is None: data['price']=0
  return data
 def save(self,commit=True):
  obj=super().save(commit=False)
  obj.duration_minutes=self.cleaned_data['duration_minutes'];obj.duration_unit=self.cleaned_data['duration_unit']
  obj.price=self.cleaned_data['price']
  if commit: obj.save()
  return obj
