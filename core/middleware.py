from django.shortcuts import redirect
from django.urls import resolve, Resolver404
class SubscriptionAccessMiddleware:
    ALLOWED={'home','login','register','logout','subscription','subscription_select','api_subscription_warning','api_voucher_login'}
    PREFIX_ALLOW=('/admin/','/static/','/api/','/p/')
    def __init__(self,get_response): self.get_response=get_response
    def __call__(self,request):
        if request.user.is_authenticated and hasattr(request.user,'business') and not any(request.path.startswith(p) for p in self.PREFIX_ALLOW):
            try: name=resolve(request.path_info).url_name
            except Resolver404: name=None
            if name not in self.ALLOWED and not request.user.business.has_access:
                return redirect('subscription')
        return self.get_response(request)
