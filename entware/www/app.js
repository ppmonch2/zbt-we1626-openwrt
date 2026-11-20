'use strict';
const area=document.getElementById('heaters'),status=document.getElementById('status');
let heaters=[],busy=false;const states=new Map();
const errors={server_busy:'Сервер занят, дождитесь завершения текущей команды.',local_connection_failed_check_wifi_and_share_link:'Не удалось прочитать прибор. Проверьте одну Wi-Fi-сеть и ссылку HOMMYN.',command_unconfirmed_may_have_been_applied:'Нет подтверждения. Команда могла дойти — проверьте состояние прибора перед повтором.',no_target_state:'Соединение установлено, но прибор не сообщил уставку.',invalid_credentials:'Неверный идентификатор или токен.',invalid_host:'Откройте сервер по IP роутера с портом 8765.'};
async function request(data){
 const options=data?{method:'POST',headers:{'Content-Type':'application/json','X-Heatbridge':'1'},body:JSON.stringify(data)}:{};
 const response=await fetch('cgi-bin/api',options);const payload=await response.json();
 if(!response.ok||!payload.ok)throw new Error(errors[payload.error]||payload.error||'Ошибка соединения');
 heaters=payload.heaters;
 if(payload.states && typeof payload.states === 'object') {
  states.clear();
  for(const h of heaters) {
   const snapshot=payload.states[h.id];
   if(h.verified && snapshot && typeof snapshot === 'object') states.set(h.id,snapshot);
  }
 }
 return payload;
}
function parseShare(text){
 const u=new URL(text.trim());
 if(!['rusklimat:','https:'].includes(u.protocol))throw new Error('Нужна ссылка «Поделиться устройством» из HOMMYN.');
 const parts=u.pathname.split('/').filter(Boolean);if(parts[0]==='device-share')parts.shift();
 const token=u.searchParams.get('token');
 if(parts.length!==3||parts[0]!=='rusclimate'||parts[1]!=='46'||!/^\w{12}$/.test(parts[2])||!/^[a-fA-F0-9]{12}$/.test(parts[2])||!/^[a-fA-F0-9]{32}$/.test(token||''))throw new Error('Поддерживается Ballu типа 46. Проверьте полную ссылку, включая token.');
 return {device_id:parts[2],token};
}
function node(tag,text,cls){const e=document.createElement(tag);if(text)e.textContent=text;if(cls)e.className=cls;return e;}
function button(text,action,disabled=false,secondary=false){const e=node('button',text,secondary?'secondary':'');e.disabled=disabled||busy;e.onclick=action;return e;}
async function operate(id,action,value){
 if(busy)return;busy=true;render();status.className='';status.textContent='Соединяюсь с обогревателем…';
 try{
  const data=await request({id,action,...(value===undefined?{}:{value})});
  if(data.result && !states.has(id))states.set(id,data.result);
  status.textContent=action==='probe'?'Состояние прочитано. Локальное управление доступно.':data.result.state_echo_matches_request?'Прибор подтвердил команду и сообщил соответствующую уставку или режим.':'Прибор подтвердил команду; новое состояние ещё не получено.';
 }catch(e){states.delete(id);status.className='error';status.textContent=e.message;}
 finally{busy=false;render();}
}
function render(){
 area.replaceChildren();
 for(const h of heaters){
  const card=node('article');card.append(node('h2',h.name));
  const s=states.get(h.id);let description=h.configured?(h.verified?'Устройство настроено. Нажмите «Проверить», чтобы получить свежие данные.':'Ссылка сохранена. Сначала прочитайте состояние.'):'Добавьте личную ссылку HOMMYN ниже.';
  if(s){
   const parts=[];
   if(typeof s.current_temperature==='number')parts.push('В комнате '+s.current_temperature+' °C');
   if(typeof s.reported_target==='number')parts.push('Уставка '+s.reported_target+' °C');
   if(typeof s.device_mode==='number')parts.push('Режим '+({0:'выключен',1:'Комфорт',2:'Эко',3:'Антизамерзание'}[s.device_mode]??s.device_mode));
   if(parts.length){
    const stamp=typeof s.observed_at==='number' ? ' · проверено '+new Date(s.observed_at*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'}) : '';
    description=parts.join(' · ')+stamp;
   } else description='Свежие показания пока недоступны. Проверьте состояние прибора.';
  }
  card.append(node('p',description,'state'));
  card.append(button('Проверить состояние',()=>operate(h.id,'probe'),!h.configured,true));
  const controls=node('div',null,'controls'),temp=node('input');temp.type='number';temp.min='10';temp.max='35';temp.step='1';temp.value=s?.reported_target??h.temp??18;temp.setAttribute('aria-label','Уставка температуры');temp.disabled=busy||!h.verified;
  controls.append(temp,node('span','°C'),button('Задать',()=>{const value=Number(temp.value);if(!Number.isInteger(value)||value<10||value>35){status.textContent='Допустимы целые температуры 10–35 °C.';return;}operate(h.id,'set',value);},!h.verified));card.append(controls);
  const power=node('div',null,'controls');power.append(button('Включить · Комфорт',()=>operate(h.id,'mode',1),!h.verified,true),button('Выключить',()=>operate(h.id,'mode',0),!h.verified,true));card.append(power);
  const details=node('details');details.append(node('summary',h.configured?'Заменить привязку':'Подключить обогреватель'));
  const name=node('input');name.type='text';name.maxLength=32;name.value=h.name;name.id='name-'+h.id;
  const nameLabel=node('label','Название');nameLabel.htmlFor=name.id;
  const share=node('input');share.type='password';share.autocomplete='off';share.placeholder='rusklimat://device-share/…';share.id='share-'+h.id;
  const shareLabel=node('label','Личная ссылка HOMMYN');shareLabel.htmlFor=share.id;
  details.append(nameLabel,name,shareLabel,share,node('p','Ссылка содержит секретный токен. Она сохраняется только на флешке роутера.','small'));
  details.append(button('Сохранить привязку',async()=>{
   if(busy)return;
   try{const credentials=parseShare(share.value);if(!name.value.trim())throw Error('Введите название.');busy=true;await request({action:'configure',id:h.id,name:name.value.trim(),...credentials});share.value='';states.delete(h.id);status.className='';status.textContent='Ссылка сохранена. Теперь нажмите «Проверить состояние».';}
   catch(e){status.className='error';status.textContent=e.message;}
   finally{busy=false;render();}
  }));card.append(details);area.append(card);
 }
 document.querySelectorAll('.presets button').forEach(b=>b.disabled=busy);
}
async function preset(value){
 if(busy)return;const ready=heaters.filter(h=>h.verified);if(!ready.length){status.textContent='Сначала настройте приборы и проверьте соединение.';return;}
 busy=true;render();let success=0;const failed=[];
 for(const h of ready){status.textContent='Отправляю '+h.name+'…';try{const data=await request({action:'set',id:h.id,value});states.set(h.id,data.result);success++;}catch(e){states.delete(h.id);failed.push(h.name);}}
 busy=false;render();status.className=failed.length?'error':'';status.textContent='Подтвердили команду: '+success+'.'+(failed.length?' Нет подтверждения: '+failed.join(', ')+'. Команды могли дойти.':' Выключенные приборы остаются выключенными.');
}
document.getElementById('away').onclick=()=>preset(12);document.getElementById('arrive').onclick=()=>preset(22);
document.getElementById('refresh').onclick=async()=>{if(busy)return;try{await request();render();status.textContent='Список обновлён. Для свежей температуры нажмите «Проверить состояние».';}catch(e){status.textContent=e.message;}};
request().then(()=>{render();status.textContent='Локальный сервер готов. Для свежих показаний включите канал обмена.';}).catch(e=>{status.className='error';status.textContent=e.message;});
// Refresh only the locally cached observations, never send a heater command.
setInterval(async()=>{if(busy)return;try{await request();render();}catch(_){/* Leave controls usable when offline. */}},30000);
