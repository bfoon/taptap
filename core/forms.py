from django import forms
from django.contrib.auth.models import User
from .models import Router,VoucherPlan
class RegisterForm(forms.Form):
 business_name=forms.CharField(max_length=180); owner_name=forms.CharField(max_length=180); phone=forms.CharField(max_length=60); email=forms.EmailField(); password=forms.CharField(widget=forms.PasswordInput,min_length=6)
class RouterForm(forms.ModelForm):
 class Meta:
  model=Router; fields=['name','ip_address','api_port','username','password','use_ssl']; widgets={'password':forms.PasswordInput(render_value=True)}
class PlanForm(forms.ModelForm):
 duration_value=forms.IntegerField(min_value=1,initial=1,label='Duration')
 duration_unit=forms.ChoiceField(choices=[('minutes','Minutes'),('hours','Hours'),('days','Days'),('months','Months (30 days)')],initial='days',label='Unit')
 field_order=['name','price','duration_value','duration_unit','max_devices','speed_limit','data_limit_mb','active']
 class Meta: model=VoucherPlan; fields=['name','price','max_devices','speed_limit','data_limit_mb','active']
 def __init__(self,*a,**kw):
  super().__init__(*a,**kw)
  if self.instance and self.instance.pk:
   from .durations import split
   v,u=split(self.instance.duration_minutes,self.instance.duration_unit)
   self.fields['duration_value'].initial=v;self.fields['duration_unit'].initial=u
 def clean(self):
  data=super().clean()
  from .durations import to_minutes
  try: data['duration_minutes']=to_minutes(data.get('duration_value'),data.get('duration_unit'))
  except ValueError as e: self.add_error('duration_value',str(e))
  return data
 def save(self,commit=True):
  obj=super().save(commit=False)
  obj.duration_minutes=self.cleaned_data['duration_minutes'];obj.duration_unit=self.cleaned_data['duration_unit']
  if commit: obj.save()
  return obj
