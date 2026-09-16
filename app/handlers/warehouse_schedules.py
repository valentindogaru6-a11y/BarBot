"""FSM UI for v10 scheduled immutable-report delivery."""
from __future__ import annotations
import asyncio
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo
from aiogram import F,Router
from aiogram.filters import Command,StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery,Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app.handlers.common import edit_or_send_text
from app.handlers.warehouse_states import WarehouseScheduleStates
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError
from app.services.warehouse.schedule_service import WarehouseScheduleService
router=Router(name=__name__)
async def _t(fn,*a,**kw):return await asyncio.to_thread(fn,*a,**kw)
def _kb(*buttons):
 b=InlineKeyboardBuilder()
 for text,action,obj,aux in buttons:b.button(text=text,callback_data=WarehouseCallback(action=action,object_id=obj,aux=aux))
 b.adjust(1);return b.as_markup()
def _error(e):return 'Недостаточно прав.' if isinstance(e,AccessDeniedError) else escape(str(e)) if isinstance(e,ValueError) else 'Не удалось сохранить расписание.'
async def _home(message,service,actor):
 rows=await _t(service.list,actor);lines=[];buttons=[]
 for row in rows:
  recipients=await _t(service.repository.recipient_ids,row['id']);job=(await _t(service.repository.jobs,row['id'],1));lines.append(f"• <b>{escape(row['name'])}</b> — {'Ежедневно' if row['schedule_type']=='daily' else 'Разово'}, {'вкл.' if row['is_active'] else 'выкл.'}, получателей: {len(recipients)}"+(f", job: {job[0]['status']}" if job else ''))
  buttons.append((f"📋 {row['name']}",'sv',row['id'],0))
 await edit_or_send_text(message,'🗓 <b>Автоотправка отчётов</b>\n\n'+('\n'.join(lines) if lines else 'Расписаний ещё нет.'),_kb(*buttons,('➕ Ежедневное расписание','sa',0,0),('➕ Разовая отправка','so',0,0),('📜 История запусков','sh',0,0)))
async def _select_recipients(message,state,service,actor):
 recipients=await _t(service.recipients,actor);data=await state.get_data();selected=set(data.get('recipient_ids',[]));buttons=[(f"{'✅' if r['id'] in selected else '⬜'} {r['title']}",'st',r['id'],0) for r in recipients];buttons += [('➡️ Далее','sp',0,0),('❌ Отмена','sx',0,0)];await edit_or_send_text(message,'👥 <b>Выберите получателей</b>',_kb(*buttons))
def _preview(data):
 names=', '.join(map(str,data.get('recipient_ids',[]))) or '—'
 if data['kind']=='daily':return f"🗓 <b>Ежедневная отправка</b>\nНазвание: {escape(data['name'])}\nЗадержка: {data['delay']} мин.\nTimezone: Europe/Kyiv\nПолучатели ID: {names}"
 return f"🗓 <b>Разовая отправка</b>\nНазвание: {escape(data['name'])}\nReport: {data['report_id']}\nUTC: <code>{data['run_at_utc']}</code>\nПолучатели ID: {names}"
@router.callback_query(WarehouseCallback.filter(F.action=='sd'))
async def home(callback:CallbackQuery,warehouse_schedule_service:WarehouseScheduleService):
 try:
  if isinstance(callback.message,Message):await _home(callback.message,warehouse_schedule_service,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer()
@router.callback_query(WarehouseCallback.filter(F.action=='sa'))
async def daily(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:await _t(warehouse_schedule_service.sources,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await state.clear();await state.update_data(kind='daily');await state.set_state(WarehouseScheduleStates.daily_name);await callback.answer();await callback.message.answer('Введите название ежедневного расписания.')
@router.message(WarehouseScheduleStates.daily_name,F.text)
async def daily_name(message:Message,state:FSMContext):
 await state.update_data(name=(message.text or '').strip());await state.set_state(WarehouseScheduleStates.daily_delay);await message.answer('Введите задержку после закрытия: 0–1440 минут.')
@router.message(WarehouseScheduleStates.daily_delay,F.text)
async def daily_delay(message:Message,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  delay=int(message.text or '');
  if not 0<=delay<=1440:raise ValueError('Допустимо 0–1440')
  sources=await _t(warehouse_schedule_service.sources,message.from_user.id)
  if not sources:raise ValueError('Нет source location')
  await state.update_data(delay=delay,source_location_id=sources[0]['id'],recipient_ids=[]);await state.set_state(WarehouseScheduleStates.recipients);await _select_recipients(message,state,warehouse_schedule_service,message.from_user.id)
 except Exception as e:await message.answer(_error(e))
@router.callback_query(WarehouseCallback.filter(F.action=='so'))
async def once(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:reports=await _t(warehouse_schedule_service.reports,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not reports:await callback.answer('Нет готовых отчётов.',show_alert=True);return
 await state.clear();await state.update_data(kind='once',report_id=reports[0]['id']);await state.set_state(WarehouseScheduleStates.once_name);await callback.answer();await callback.message.answer(f"Введите название разовой отправки для report #{reports[0]['id']}.")
@router.message(WarehouseScheduleStates.once_name,F.text)
async def once_name(message:Message,state:FSMContext):await state.update_data(name=(message.text or '').strip());await state.set_state(WarehouseScheduleStates.once_date);await message.answer('Введите дату: ДД.ММ.ГГГГ')
@router.message(WarehouseScheduleStates.once_date,F.text)
async def once_date(message:Message,state:FSMContext):await state.update_data(date=message.text or '');await state.set_state(WarehouseScheduleStates.once_time);await message.answer('Введите время: ЧЧ:ММ')
@router.message(WarehouseScheduleStates.once_time,F.text)
async def once_time(message:Message,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  data=await state.get_data();run_at=await _t(warehouse_schedule_service.local_run_at,data['date'],message.text or '');await state.update_data(run_at_utc=run_at,recipient_ids=[]);await state.set_state(WarehouseScheduleStates.recipients);await _select_recipients(message,state,warehouse_schedule_service,message.from_user.id)
 except Exception as e:await message.answer(_error(e))
@router.callback_query(WarehouseCallback.filter(F.action=='st'),StateFilter(WarehouseScheduleStates.recipients))
async def toggle_recipient(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 data=await state.get_data();selected=set(data.get('recipient_ids',[]));selected.symmetric_difference_update({callback_data.object_id});await state.update_data(recipient_ids=list(selected));await callback.answer();await _select_recipients(callback.message,state,warehouse_schedule_service,callback.from_user.id)
@router.callback_query(WarehouseCallback.filter(F.action=='sp'),StateFilter(WarehouseScheduleStates.recipients))
async def preview(callback:CallbackQuery,state:FSMContext):
 data=await state.get_data()
 if not data.get('recipient_ids'):await callback.answer('Выберите хотя бы одного получателя.',show_alert=True);return
 await state.set_state(WarehouseScheduleStates.confirm);await callback.answer();await edit_or_send_text(callback.message,_preview(data),_kb(('✅ Создать','sf',0,0),('❌ Отмена','sx',0,0)))
@router.callback_query(WarehouseCallback.filter(F.action=='sf'),StateFilter(WarehouseScheduleStates.confirm))
async def save(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  d=await state.get_data();values={'name':d['name'],'schedule_type':d['kind'],'timezone':'Europe/Kyiv','max_wait':1440,'is_active':1}
  if d['kind']=='daily':values|={'source_location_id':d['source_location_id'],'delay':d['delay']}
  else:values|={'report_id':d['report_id'],'run_at_utc':d['run_at_utc']}
  await _t(warehouse_schedule_service.create,values,d['recipient_ids'],callback.from_user.id);await state.clear();await callback.answer('Расписание создано');await _home(callback.message,warehouse_schedule_service,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True)
@router.callback_query(WarehouseCallback.filter(F.action=='sx'))
async def cancel(callback:CallbackQuery,state:FSMContext):await state.clear();await callback.answer('Отменено')
@router.message(Command('cancel'),StateFilter(WarehouseScheduleStates))
async def command_cancel(message:Message,state:FSMContext):await state.clear();await message.answer('Работа с расписанием отменена.')

def _card(row):
 kind='Ежедневно' if row['schedule_type']=='daily' else 'Разово';target=f"delay: {row['delay_minutes_after_close']} мин." if row['schedule_type']=='daily' else f"UTC: {row['run_at_utc']}"
 return f"🗓 <b>{escape(row['name'])}</b>\n\nТип: {kind}\nСтатус: {'включено' if row['is_active'] else 'выключено'}\n{target}\nTimezone: {row['timezone']}\nПолучателей: {len(row['recipients'])}\nRevision: {row['revision']}\nПоследний job: {row['latest_job']['status'] if row['latest_job'] else 'нет'}"
@router.callback_query(WarehouseCallback.filter(F.action=='sv'))
async def card(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:row=await _t(warehouse_schedule_service.details,callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not row:await callback.answer('Расписание не найдено.',show_alert=True);return
 await callback.answer();buttons=[('📜 Запуски','sj',row['id'],0),('⬅️ Назад','sd',0,0)]
 try:
  await _t(warehouse_schedule_service.list,callback.from_user.id)
  time_label='⏱ Дата и время' if row['schedule_type']=='once' else '⏱ Задержка'
  buttons[:0]=[('✏️ Название','en',row['id'],row['revision']),(time_label,'ed',row['id'],row['revision']),('👥 Получатели','er',row['id'],row['revision']),('⏸ Выключить' if row['is_active'] else '▶️ Включить','se',row['id'],row['revision'])]
 except Exception:pass
 await edit_or_send_text(callback.message,_card(row),_kb(*buttons))
@router.callback_query(WarehouseCallback.filter(F.action=='se'))
async def enable(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:
  row=await _t(warehouse_schedule_service.repository.details,callback_data.object_id);updated=await _t(warehouse_schedule_service.update,row['id'],{'is_active':0 if row['is_active'] else 1},row['recipients'],callback_data.aux,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer('Статус обновлён');await card(callback,callback_data,warehouse_schedule_service)
@router.callback_query(WarehouseCallback.filter(F.action=='sj'))
async def jobs(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:rows=await _t(warehouse_schedule_service.jobs,callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer();buttons=[(f"{r['business_date']} — {r['status']}",'sk',r['id'],0) for r in rows[:8]]+[('⬅️ К расписанию','sv',callback_data.object_id,0)]
 await edit_or_send_text(callback.message,'📜 <b>Запуски</b>',_kb(*buttons))
@router.callback_query(WarehouseCallback.filter(F.action=='sk'))
async def job(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:r=await _t(warehouse_schedule_service.repository.job,callback_data.object_id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not r:await callback.answer('Запуск не найден.',show_alert=True);return
 await callback.answer();buttons=[('⬅️ К запускам','sj',r['schedule_id'],0)]
 if r['status'] in ('failed','partial_failed'):buttons.insert(0,('🔁 Повторить неудачные','sr',r['id'],0))
 if r['shift_id'] is None and r['status'] in ('scheduled','waiting_report'):buttons.insert(0,('❌ Отменить запуск','sc',r['id'],0))
 await edit_or_send_text(callback.message,f"📄 <b>Запуск</b>\n\nДата: {r['business_date']}\nВремя: {r['scheduled_at_utc']}\nСтатус: {r['status']}\nОшибка: {r['last_error_code'] or '—'}",_kb(*buttons))
@router.callback_query(WarehouseCallback.filter(F.action=='sc'))
async def cancel_job(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 job=await _t(warehouse_schedule_service.repository.job,callback_data.object_id)
 if not job or job['shift_id'] is not None or job['status'] not in ('scheduled','waiting_report'):await callback.answer('Этот запуск нельзя отменить',show_alert=True);return
 await callback.answer();await edit_or_send_text(callback.message,f"❌ <b>Отменить разовый запуск?</b>\n\nДата: {job['business_date']}\nВремя: {job['scheduled_at_utc']}",_kb(('❌ Отменить запуск','cq',job['id'],0),('↩️ Не отменять','cx',job['id'],0)))

@router.callback_query(WarehouseCallback.filter(F.action=='cq'))
async def cancel_confirm(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:ok=await _t(warehouse_schedule_service.cancel_job,callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer('Запуск отменён' if ok else 'Запуск уже нельзя отменить');await job(callback,callback_data,warehouse_schedule_service)
@router.callback_query(WarehouseCallback.filter(F.action=='cx'))
async def cancel_back(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):await job(callback,callback_data,warehouse_schedule_service)

@router.callback_query(WarehouseCallback.filter(F.action=='sr'))
async def retry_preview(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:r=await _t(warehouse_schedule_service.repository.job,callback_data.object_id);await _t(warehouse_schedule_service.details,r['schedule_id'],callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not r or r['status'] not in ('failed','partial_failed'):await callback.answer('Этот запуск нельзя повторить',show_alert=True);return
 await callback.answer();await edit_or_send_text(callback.message,f"🔁 <b>Повторить неудачные доставки?</b>\n\nДата: {r['business_date']}\nReport: готовый immutable XLSX",_kb(('🔁 Повторить','rq',r['id'],0),('❌ Отмена','rx',r['id'],0)))
@router.callback_query(WarehouseCallback.filter(F.action=='rq'))
async def retry_confirm(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:result=await warehouse_schedule_service.retry_job(callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer(f"Доставлено: {result['delivered']}, ошибок: {result['failed']}");await job(callback,callback_data,warehouse_schedule_service)
@router.callback_query(WarehouseCallback.filter(F.action=='rx'))
async def retry_back(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):await job(callback,callback_data,warehouse_schedule_service)

async def _edit_start(callback,data,state,service,kind,prompt):
 try:row=await _t(service.repository.details,data.object_id);await _t(service.list,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not row or row['revision']!=data.aux:await callback.answer('Данные изменились. Откройте карточку заново.',show_alert=True);return
 await state.clear();await state.update_data(edit_schedule=row['id'],edit_revision=row['revision'],edit_kind=kind,edit_recipients=list(row['recipients']));await state.set_state(WarehouseScheduleStates.edit_name if kind=='name' else WarehouseScheduleStates.edit_delay);await callback.answer();await callback.message.answer(prompt)
@router.callback_query(WarehouseCallback.filter(F.action=='en'))
async def edit_name_start(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):await _edit_start(callback,callback_data,state,warehouse_schedule_service,'name','Введите новое название (1–200 символов).')
@router.callback_query(WarehouseCallback.filter(F.action=='ed'))
async def edit_delay_start(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:row=await _t(warehouse_schedule_service.repository.details,callback_data.object_id);await _t(warehouse_schedule_service.list,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not row or row['revision']!=callback_data.aux:await callback.answer('Данные изменились. Откройте карточку заново.',show_alert=True);return
 await state.clear();await state.update_data(edit_schedule=row['id'],edit_revision=row['revision'],edit_recipients=list(row['recipients']),old_run_at=row['run_at_utc'])
 if row['schedule_type']=='once':
  report=await _t(warehouse_schedule_service.repository.report_summary,row['report_id']);await state.update_data(report_date=report['business_date'] if report else '—',report_version=report['version'] if report else '—');await state.set_state(WarehouseScheduleStates.edit_once_date);prompt='Введите новую дату: ДД.ММ.ГГГГ'
 else:await state.set_state(WarehouseScheduleStates.edit_delay);prompt='Введите новую задержку 0–1440 минут.'
 await callback.answer();await callback.message.answer(prompt)
@router.message(WarehouseScheduleStates.edit_name,F.text)
async def edit_name_input(message:Message,state:FSMContext):
 value=(message.text or '').strip()
 if not 1<=len(value)<=200:await message.answer('Название должно содержать 1–200 символов.');return
 await state.update_data(edit_values={'name':value});await state.set_state(WarehouseScheduleStates.edit_confirm);await message.answer(f"Новое название: <b>{escape(value)}</b>",reply_markup=_kb(('✅ Сохранить','es',0,0),('❌ Отмена','sx',0,0)))
@router.message(WarehouseScheduleStates.edit_delay,F.text)
async def edit_delay_input(message:Message,state:FSMContext):
 try:value=int(message.text or '');assert 0<=value<=1440
 except Exception:await message.answer('Допустимо целое число 0–1440.');return
 await state.update_data(edit_values={'delay':value});await state.set_state(WarehouseScheduleStates.edit_confirm);await message.answer(f"Новая задержка: <b>{value}</b> мин.",reply_markup=_kb(('✅ Сохранить','es',0,0),('❌ Отмена','sx',0,0)))
@router.callback_query(WarehouseCallback.filter(F.action=='es'),StateFilter(WarehouseScheduleStates.edit_confirm))
async def edit_save(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  d=await state.get_data();await _t(warehouse_schedule_service.update,d['edit_schedule'],d['edit_values'],d['edit_recipients'],d['edit_revision'],callback.from_user.id);await state.clear();await callback.answer('Сохранено');await card(callback,WarehouseCallback(action='sv',object_id=d['edit_schedule']),warehouse_schedule_service)
 except Exception as e:await callback.answer(_error(e),show_alert=True)

async def _edit_recipient_page(message,state,service,actor,page=0):
 recipients=await _t(service.recipients,actor);data=await state.get_data();active_ids={r['id'] for r in recipients};selected=set(data.get('edit_selected',[]))&active_ids;await state.update_data(edit_selected=list(selected));page=max(0,min(page,max(0,(len(recipients)-1)//8)));rows=recipients[page*8:(page+1)*8]
 buttons=[(f"{'✅' if r['id'] in selected else '⬜'} {r['title']}",'rt',r['id'],page) for r in rows]
 if page>0:buttons.append(('⬅️','rp',0,page-1))
 if (page+1)*8<len(recipients):buttons.append(('➡️','rp',0,page+1))
 buttons += [('👁 Предпросмотр','rv',0,page),('❌ Отмена','sx',0,0)]
 await edit_or_send_text(message,f"👥 <b>Получатели</b> · страница {page+1}",_kb(*buttons))
@router.callback_query(WarehouseCallback.filter(F.action=='er'))
async def edit_recipients_start(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:row=await _t(warehouse_schedule_service.repository.details,callback_data.object_id);await _t(warehouse_schedule_service.list,callback.from_user.id);active=await _t(warehouse_schedule_service.recipients,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 if not row or row['revision']!=callback_data.aux:await callback.answer('Данные изменились. Откройте карточку заново.',show_alert=True);return
 active_ids={r['id'] for r in active};await state.clear();await state.update_data(edit_schedule=row['id'],edit_revision=row['revision'],edit_old=list(row['recipients']),edit_selected=[r for r in row['recipients'] if r in active_ids]);await state.set_state(WarehouseScheduleStates.edit_recipients);await callback.answer();await _edit_recipient_page(callback.message,state,warehouse_schedule_service,callback.from_user.id)
@router.callback_query(WarehouseCallback.filter(F.action=='rt'),StateFilter(WarehouseScheduleStates.edit_recipients))
async def edit_recipient_toggle(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 data=await state.get_data();selected=set(data.get('edit_selected',[]));selected.symmetric_difference_update({callback_data.object_id});await state.update_data(edit_selected=list(selected));await callback.answer();await _edit_recipient_page(callback.message,state,warehouse_schedule_service,callback.from_user.id,callback_data.aux)
@router.callback_query(WarehouseCallback.filter(F.action=='rp'),StateFilter(WarehouseScheduleStates.edit_recipients))
async def edit_recipient_page(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):await callback.answer();await _edit_recipient_page(callback.message,state,warehouse_schedule_service,callback.from_user.id,callback_data.aux)
@router.callback_query(WarehouseCallback.filter(F.action=='rv'),StateFilter(WarehouseScheduleStates.edit_recipients))
async def edit_recipient_preview(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 data=await state.get_data();selected=data.get('edit_selected',[])
 if not selected:await callback.answer('Выберите хотя бы одного получателя.',show_alert=True);return
 old=await _t(warehouse_schedule_service.repository.recipients_by_ids,data.get('edit_old',[]));new=await _t(warehouse_schedule_service.repository.recipients_by_ids,selected)
 text='👥 <b>Изменение получателей</b>\n\nБыло:\n'+'\n'.join('• '+escape(r['title']) for r in old)+'\n\nСтанет:\n'+'\n'.join('• '+escape(r['title']) for r in new)
 await callback.answer();await edit_or_send_text(callback.message,text,_kb(('✅ Сохранить','rz',0,0),('❌ Отмена','sx',0,0)))
@router.callback_query(WarehouseCallback.filter(F.action=='rz'),StateFilter(WarehouseScheduleStates.edit_recipients))
async def edit_recipient_save(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  d=await state.get_data();await _t(warehouse_schedule_service.update_recipients,d['edit_schedule'],d['edit_selected'],d['edit_revision'],callback.from_user.id);sid=d['edit_schedule'];await state.clear();await callback.answer('Получатели сохранены');await card(callback,WarehouseCallback(action='sv',object_id=sid),warehouse_schedule_service)
 except Exception as e:
  await callback.answer(_error(e),show_alert=True)
  try:await _edit_recipient_page(callback.message,state,warehouse_schedule_service,callback.from_user.id)
  except Exception:pass

@router.message(WarehouseScheduleStates.edit_once_date,F.text)
async def edit_once_date_input(message:Message,state:FSMContext):await state.update_data(edit_date=message.text or '');await state.set_state(WarehouseScheduleStates.edit_once_time);await message.answer('Введите новое время: ЧЧ:ММ')
def _local_time(stamp):
 return datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m.%Y %H:%M %Z')
async def _once_preview(message,state,run_at):
 data=await state.get_data();await state.update_data(edit_run_at=run_at);await state.set_state(WarehouseScheduleStates.edit_confirm)
 text=(f"⏱ <b>Изменение даты и времени</b>\n\n"
       f"Было: <b>{_local_time(data['old_run_at'])}</b>\nUTC: <code>{data['old_run_at']}</code>\n\n"
       f"Станет: <b>{_local_time(run_at)}</b>\nUTC: <code>{run_at}</code>\n\n"
       f"Отчёт: {escape(str(data['report_date']))}, версия {escape(str(data['report_version']))}")
 await message.answer(text,reply_markup=_kb(('✅ Сохранить','os',0,0),('❌ Отмена','sx',0,0)))
@router.message(WarehouseScheduleStates.edit_once_time,F.text)
async def edit_once_time_input(message:Message,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  data=await state.get_data();values=await _t(warehouse_schedule_service.local_run_candidates,data['edit_date'],message.text or '')
 except Exception as e:await message.answer(_error(e));return
 if len(values)==1:await _once_preview(message,state,values[0]);return
 await state.update_data(edit_occurrences=list(values));await state.set_state(WarehouseScheduleStates.edit_once_occurrence);await message.answer('Это время встречается дважды. Выберите вхождение:',reply_markup=_kb(('1️⃣ Первое вхождение','o1',0,0),('2️⃣ Второе вхождение','o2',0,0),('❌ Отмена','sx',0,0)))
@router.callback_query(WarehouseCallback.filter(F.action.in_({'o1','o2'})),StateFilter(WarehouseScheduleStates.edit_once_occurrence))
async def edit_once_occurrence(callback:CallbackQuery,callback_data:WarehouseCallback,state:FSMContext):
 data=await state.get_data();await callback.answer();await _once_preview(callback.message,state,data['edit_occurrences'][0 if callback_data.action=='o1' else 1])
@router.callback_query(WarehouseCallback.filter(F.action=='os'),StateFilter(WarehouseScheduleStates.edit_confirm))
async def edit_once_save(callback:CallbackQuery,state:FSMContext,warehouse_schedule_service:WarehouseScheduleService):
 try:
  d=await state.get_data();await _t(warehouse_schedule_service.update_once_datetime,d['edit_schedule'],d['edit_run_at'],d['edit_revision'],callback.from_user.id);sid=d['edit_schedule'];await state.clear();await callback.answer('Дата и время сохранены');await card(callback,WarehouseCallback(action='sv',object_id=sid),warehouse_schedule_service)
 except Exception as e:await callback.answer(_error(e),show_alert=True)
