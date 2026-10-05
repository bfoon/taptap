(function(){
  const holder=document.getElementById('memberArrearsData');
  if(!holder)return;
  let payload={members:{}};
  try{payload=JSON.parse(holder.textContent||'{}')}catch(e){return}
  const map=payload.members||{};

  function esc(s){
    return String(s??'').replace(/[&<>"']/g,c=>(
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
    ));
  }

  document.querySelectorAll('table tbody tr').forEach(tr=>{
    const cells=tr.querySelectorAll(':scope > td');
    if(cells.length<5)return;
    const memberLink=cells[0].querySelector('a');
    if(!memberLink)return;
    const code=(memberLink.textContent||'').trim().toLowerCase();
    const info=map[code];
    if(!info)return;

    const ends=cells[3];
    const actions=cells[4];

    if(info.progress!==null && info.progress!==undefined && !ends.querySelector('.member-time-mini')){
      const pct=Math.max(0,Math.min(100,Number(info.progress)||0));
      const cls=pct<=20?'low':(pct<=50?'warn':'');
      const bar=document.createElement('div');
      bar.className='member-time-mini '+cls;
      bar.title=pct+'% of the member plan period remains';
      bar.innerHTML='<div class="track"><span style="width:'+pct+'%"></span></div><small>'+pct+'%</small>';
      const left=ends.querySelector('small.text-secondary');
      if(left) left.insertAdjacentElement('afterend',bar);
      else ends.appendChild(bar);
    }

    const due=Number(info.arrears||0);
    if(due>0 && !ends.querySelector('.member-arrears-line')){
      const ar=document.createElement('small');
      ar.className='member-arrears-line';
      ar.innerHTML='<i class="bi bi-exclamation-circle"></i> Arrears '+esc(info.arrears_display);
      ends.appendChild(ar);
    }

    if(info.receipt_url && !actions.querySelector('.member-receipt-btn')){
      const print=document.createElement('a');
      print.className='btn btn-sm btn-outline-secondary member-receipt-btn';
      print.href=info.receipt_url;
      print.target='_blank';
      print.rel='noopener';
      print.title='Print latest member receipt';
      print.setAttribute('aria-label','Print latest member receipt');
      print.innerHTML='<i class="bi bi-printer"></i>';
      const first=actions.firstElementChild;
      if(first)actions.insertBefore(print,first);
      else actions.appendChild(print);
    }

    if(due>0){
      const renew=[...actions.querySelectorAll('button')].find(
        b=>(b.textContent||'').trim().toLowerCase()==='renew'
      );
      if(renew){
        renew.dataset.balance='1';
        renew.dataset.price=info.arrears;
        renew.dataset.dur='Outstanding balance only — no membership time will be added';
        renew.classList.remove('btn-outline-success');
        renew.classList.add('btn-warning');
        renew.innerHTML='<i class="bi bi-cash-coin"></i> Collect balance';
        renew.title='Collect outstanding member arrears without adding more time';
      }
    }
  });

  const modal=document.getElementById('renewModal');
  if(!modal)return;
  const form=modal.querySelector('form');
  if(!form)return;

  let mode=form.querySelector('input[name="mode"]');
  if(!mode){
    mode=document.createElement('input');
    mode.type='hidden';
    mode.name='mode';
    mode.value='renew';
    form.appendChild(mode);
  }

  let note=document.getElementById('arrearsModeNote');
  if(!note){
    note=document.createElement('div');
    note.id='arrearsModeNote';
    const body=modal.querySelector('.modal-body');
    if(body)body.insertBefore(note,body.firstChild);
  }

  modal.addEventListener('show.bs.modal',event=>{
    const trigger=event.relatedTarget;
    const amount=form.querySelector('input[name="amount"]');
    const title=modal.querySelector('.modal-title');
    if(trigger && trigger.dataset.balance==='1'){
      mode.value='balance';
      const due=trigger.dataset.price||'0.00';
      if(title)title.textContent='Collect member balance';
      if(amount){
        amount.value=due;
        amount.max=due;
        amount.min='0.01';
      }
      note.innerHTML='<b>Balance collection only.</b> This payment goes to Finance and reduces arrears. It does not extend the member expiry date.';
      note.classList.add('show');
    }else{
      mode.value='renew';
      if(amount){
        amount.removeAttribute('max');
        amount.min='0';
      }
      note.classList.remove('show');
    }
  });
})();