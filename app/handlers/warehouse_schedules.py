"""FSM UI for v10 scheduled immutable-report delivery."""
from __future__ import annotations
import asyncio
from html import escape
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
async def command_cancel(message:Message,state:FSMContext):await state.clear();await message.answer('Создание расписания отменено.')

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
  buttons.insert(0,('⏸ Выключить' if row['is_active'] else '▶️ Включить','se',row['id'],row['revision']))
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
@router.callback_query(WarehouseCallback.filter(F.action=='sr'))
async def retry(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:result=await warehouse_schedule_service.retry_job(callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer(f"Доставлено: {result['delivered']}, ошибок: {result['failed']}")
@router.callback_query(WarehouseCallback.filter(F.action=='sc'))
async def cancel_job(callback:CallbackQuery,callback_data:WarehouseCallback,warehouse_schedule_service:WarehouseScheduleService):
 try:ok=await _t(warehouse_schedule_service.cancel_job,callback_data.object_id,callback.from_user.id)
 except Exception as e:await callback.answer(_error(e),show_alert=True);return
 await callback.answer('Запуск отменён' if ok else 'Этот запуск нельзя отменить')
