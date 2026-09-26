def business_context(request):
 b=getattr(request.user,'business',None) if getattr(request,'user',None) and request.user.is_authenticated else None
 return {'current_business':b}
