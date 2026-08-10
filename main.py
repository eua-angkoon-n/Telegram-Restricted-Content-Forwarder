import sys
import asyncio
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QProgressDialog, QInputDialog
)
from PyQt6.QtCore import Qt
from panel import Ui_MainWindow
from qasync import QEventLoop, asyncSlot
from func import telegram_panel
from code_dialog import CodeDialog, AsyncMessageBox
from pyrogram import (Client,errors,enums)
import os, shutil, sqlite3, traceback , time , json , re
from datetime import datetime

os.makedirs('data', exist_ok=True)
os.makedirs('account', exist_ok=True)
os.makedirs('delete', exist_ok=True)


Status = False
Extract = False

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ui = Ui_MainWindow()
        
        self.ui.setupUi(self)
        self.setFixedSize(self.size())
        self.acclistupdate()
        self.ui.add_account.clicked.connect(self.add_account_proc)
        self.ui.remove_account_bot.clicked.connect(self.remove_account)
        self.ui.update_number_bot.clicked.connect(self.acclistupdate)
        self.ui.btn_start_forward.clicked.connect(self.forward_Channel)
        self.ui.btn_stop_forward.clicked.connect(self.disable_forward_Channel)
        self.ui.btn_pick_source.clicked.connect(lambda: self.pick_channel(self.ui.source_channel_input))
        self.ui.btn_pick_dest.clicked.connect(lambda: self.pick_channel(self.ui.dest_channel_input))
        self.ui.tab_account.currentChanged.connect(self.update_list_tab)
    
    
    
    def update_list_tab(self, index):
        if index == 0:
            r = telegram_panel.list_accounts()
            self.ui.list_account_ac.clear()
            self.ui.list_account_ac.addItems(r)
            self.ui.lcdNumber.display(len(r))
        if index == 1:
            r = telegram_panel.list_accounts()
            self.ui.combo_select_account.clear()
            self.ui.combo_select_account.addItems(r)
        return
    
    
    
    @asyncSlot()
    async def ask_code_dialog(self, title, label):
        dlg = CodeDialog(title, label, self)
        dlg.setModal(True)
        dlg.show()
        while dlg.result() == 0:
            await asyncio.sleep(0.1)

        if dlg.result() == 1:
            return dlg.get_value(), True
        else:
            return "", False
    
    
    @asyncSlot()
    async def show_async_message(self, title, message, icon=QMessageBox.Icon.Information):
        dlg = AsyncMessageBox(title, message, icon, self)
        dlg.show()

        while dlg.result is None:
            await asyncio.sleep(0.05)

        return dlg


    def do_long_task(self):
        dlg = QProgressDialog("Processing ...", None, 0, 0, self)
        dlg.setWindowTitle("Please wait.")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        return dlg


    @asyncSlot()
    async def add_account_proc(self):
        phone = self.ui.account_input_add.text().strip()

        if len(phone) < 4:
            # QMessageBox.critical(self, "Wrong", "Phone number is too short.")
            await self.show_async_message("Wrong", "Phone number is too short.", icon=QMessageBox.Icon.Critical)

            return

        if not phone.startswith("+") or not phone[1:].isdigit():
            # QMessageBox.critical(self, "Wrong", "Phone number must start with '+' and contain only digits after it.")
            await self.show_async_message("Wrong", "Phone number must start with '+' and contain only digits after it.", icon=QMessageBox.Icon.Critical)
            return

        if phone == "+123456789":
            # QMessageBox.critical(self, "Wrong", "Sample phone number is not allowed.")
            await self.show_async_message("Wrong", "Sample phone number is not allowed.", icon=QMessageBox.Icon.Critical)
            return

        dlg = self.do_long_task()
        r = await telegram_panel.add_account(phone)
        dlg.close()

        if not r["status"]:
            # QMessageBox.critical(self, "Error", r["message"])
            await self.show_async_message("Error", str(r["message"]), icon=QMessageBox.Icon.Critical)
            return

        # ورود کد
        for _ in range(3):
            # text, ok = QInputDialog.getText(self, "Account login code", "Enter the 5-digit code:")
            text, ok = await self.ask_code_dialog( "Account login code", "Enter the 5-digit code:")
            for _ in range(10):
                if not ok:
                    break
                if text.isdigit() and len(text) == 5:
                    break
                else:
                    # text, ok = QInputDialog.getText(self, "Account login code", "Enter the 5-digit code:")
                    text, ok = await self.ask_code_dialog( "Account login code", "Enter the 5-digit code:")

            if not ok:
                await telegram_panel.cancel_acc(r["cli"], r["phone"])
                # QMessageBox.critical(self, "Error", "Canceled by user.")
                await self.show_async_message("Error", "Canceled by user.", icon=QMessageBox.Icon.Critical)
                return

            dlg = self.do_long_task()
            rs = await telegram_panel.get_code(r["cli"], r["phone"], r["code_hash"], text)
            dlg.close()

            if rs["status"]:
                # QMessageBox.information(self, "Success", rs["message"])
                await self.show_async_message("Success", rs["message"], icon=QMessageBox.Icon.Information)
                telegram_panel.make_json_data(r["phone"], r["api_id"], r["api_hash"], r["proxy"], "")
                return

            if rs["message"] == "invalid_code":
                # QMessageBox.critical(self, "Error", "Invalid code.")
                await self.show_async_message("Error", "Invalid code.", icon=QMessageBox.Icon.Critical)
                continue

            if rs["message"] == "FA2":
                for _ in range(3):
                    # text, ok = QInputDialog.getText(self, "Account password", "Enter the password:")
                    text, ok = await self.ask_code_dialog("Account password", "Enter the password:")
                    if not ok:
                        await telegram_panel.cancel_acc(r["cli"], r["phone"])
                        # QMessageBox.critical(self, "Error", "Canceled by user.")
                        await self.show_async_message("Error", "Canceled by user.", icon=QMessageBox.Icon.Critical)
                        return

                    dlg = self.do_long_task()
                    rsp = await telegram_panel.get_password(r["cli"], r["phone"], text)
                    dlg.close()

                    if rsp["status"]:
                        # QMessageBox.information(self, "Success", rsp["message"])
                        await self.show_async_message("Success", rsp["message"], icon=QMessageBox.Icon.Information)
                        telegram_panel.make_json_data(r["phone"], r["api_id"], r["api_hash"], r["proxy"], text)
                        return

                    if rsp["message"] == "invalid_password":
                        # QMessageBox.critical(self, "Error", "Invalid password.")
                        await self.show_async_message("Error", "Invalid password.", icon=QMessageBox.Icon.Critical)
                        continue
                    else:
                        # QMessageBox.critical(self, "Error", rsp["message"])
                        await self.show_async_message("Error", rsp["message"], icon=QMessageBox.Icon.Critical)
                        return

            if rs["message"]:
                # QMessageBox.critical(self, "Error", rs["message"])
                await self.show_async_message("Error", rs["message"], icon=QMessageBox.Icon.Critical)
                return

        try:await telegram_panel.cancel_acc(r["cli"], r["phone"])
        except:pass
        # QMessageBox.critical(self, "Error", "Canceled by user.")
        await self.show_async_message("Error", "Canceled by user.", icon=QMessageBox.Icon.Critical)
        return

    def remove_account(self):
        phone = self.ui.remove_account_input.text().strip()
        if phone in telegram_panel.list_accounts():
            telegram_panel.remove_account(phone)
            QMessageBox.information(self, "Success", "Account removed.")
        else:
            QMessageBox.critical(self, "Error", "Account not found.")
        return
    

    def acclistupdate(self,log=True):
        r = telegram_panel.list_accounts()
        self.ui.list_account_ac.clear()
        self.ui.list_account_ac.addItems(r)
        self.ui.lcdNumber.display(len(r))
        if not log:
            QMessageBox.information(self, "Success", "Account list updated.")
        return
    
        
    
    @asyncSlot()
    async def pick_channel(self, target_input):
        phone = self.ui.combo_select_account.currentText()
        if not phone:
            await self.show_async_message("Error", "Select an account first.", icon=QMessageBox.Icon.Critical)
            return

        data = telegram_panel.get_json_data(phone)
        if data is None:
            await self.show_async_message("Error", "No saved data found for account {}.".format(phone), icon=QMessageBox.Icon.Critical)
            return

        proxy = await telegram_panel.get_proxy(data["proxy"])
        cli = Client('account/{}'.format(phone), data["api_id"], data["api_hash"], proxy=proxy[0])

        chats = []
        try:
            await asyncio.wait_for(cli.connect(), 15)
            async for dialog in cli.get_dialogs():
                chat = dialog.chat
                if chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP, enums.ChatType.CHANNEL):
                    chats.append((chat.title or str(chat.id), chat.id))
        except Exception as e:
            await self.show_async_message("Error", "Failed to load chats: {}".format(e), icon=QMessageBox.Icon.Critical)
            return
        finally:
            try:await cli.disconnect()
            except:pass

        if not chats:
            await self.show_async_message("Error", "No groups/channels found for this account.", icon=QMessageBox.Icon.Critical)
            return

        items = ["{} ({})".format(title, chat_id) for title, chat_id in chats]
        choice, ok = QInputDialog.getItem(self, "Choose a chat", "Groups/Channels:", items, editable=False)
        if ok and choice:
            selected_id = chats[items.index(choice)][1]
            target_input.setText(str(selected_id))
        return


    @asyncSlot()
    async def disable_forward_Channel(self):
        global Extract
        if Extract:
            Extract = False
            self.ui.lbl_status.setText("Status: Inactive")
            # QMessageBox.information(self, "Success", "Extraction stopped.")
            await self.show_async_message("Success", "Extraction stopped.", icon=QMessageBox.Icon.Information)
        else:
            # QMessageBox.critical(self, "Error", "Extraction is not active.")
            await self.show_async_message("Error", "Extraction is not active.", icon=QMessageBox.Icon.Critical)
        return
        
    
    @asyncSlot()
    async def forward_Channel(self):
        global Extract
        
        self.ui.forward_log.clear()
        self.ui.forward_log.setReadOnly(True)
        
        if len(telegram_panel.list_accounts()) == 0:
            # QMessageBox.critical(self, "Error", "No accounts found.")
            await self.show_async_message("Error", "No accounts found.", icon=QMessageBox.Icon.Critical)
            return
        if Extract:
            # QMessageBox.critical(self, "Error", "Already extracting.")
            await self.show_async_message("Error", "Already extracting.", icon=QMessageBox.Icon.Critical)
            return
        
        phone = self.ui.combo_select_account.currentText()
        
        fromchat = self.ui.source_channel_input.text().strip()
        forchat = self.ui.dest_channel_input.text().strip()

        def is_valid_target(text):
            return telegram_panel.is_valid_telegram_link(text) or telegram_panel.is_valid_chat_id(text)

        if is_valid_target(fromchat) and is_valid_target(forchat):
            Extract = True
            self.ui.lbl_status.setText("Status: Active")
            asyncio.create_task(self.forward_proc(phone,fromchat,forchat))
        else:
            # QMessageBox.critical(self, "Error", "Invalid Telegram link.")
            await self.show_async_message("Error", "Invalid Telegram link.", icon=QMessageBox.Icon.Critical)
        return
    
    
    async def forward_proc(self,phone , fromchat , forchat):
        global Extract
        
        
        self.ui.forward_log.appendPlainText("Extracting {}...".format(phone))
        data = telegram_panel.get_json_data(phone)
        if data is None:
            Extract = False
            self.ui.lbl_status.setText("Status: Disactive")
            self.ui.forward_log.appendPlainText("No saved data found for account {}.".format(phone))
            await self.show_async_message("Error", "No saved data found for account {}.".format(phone), icon=QMessageBox.Icon.Critical)
            return
        proxy = await telegram_panel.get_proxy(data["proxy"])
        cli = Client('account/{}'.format(phone), data["api_id"], data["api_hash"], proxy=proxy[0])
        await asyncio.wait_for(cli.connect() , 15)
        cli.me = await cli.get_me()
        self.ui.forward_log.appendPlainText("Connected to {}.".format(phone))
        async for _ in cli.get_dialogs():
            pass
        if telegram_panel.is_valid_chat_id(fromchat) :
            chtd = int(fromchat)
            join = await telegram_panel.get_chat(cli,chtd)
        else:
            join = await telegram_panel.Join(cli,fromchat)
        if telegram_panel.is_valid_chat_id(forchat) :
            chtdfor = int(forchat)
            joinfor = await telegram_panel.get_chat(cli,chtdfor)
        else:
            joinfor = await telegram_panel.Join(cli,forchat)
        if len(join) != 3 or len(joinfor) != 3:
            Extract = False
            try:await cli.disconnect()
            except:pass
            # QMessageBox.critical(self, "Error", "Failed to join the channel.")
            error = join[0] if len(join) != 3 else joinfor[0]
            self.ui.forward_log.appendPlainText("Failed to join the channel.\n{}".format(error))
            await self.show_async_message("Error", "Failed to join the channel.", icon=QMessageBox.Icon.Critical)
            return
        chat = await cli.get_chat(join[0])
        chatfor = await cli.get_chat(joinfor[0])

        ana_count = 0
        okmsg = 0
        badmsg = 0
        activated_at = datetime.now()

        last_id = 0
        async for m in cli.get_chat_history(chat_id=chat.id, limit=1):
            last_id = m.id

        async def process_message(message):
            nonlocal ana_count, okmsg, badmsg
            if message.date and message.date < activated_at:
                return
            ana_count += 1
            try:
                if message.photo:
                    path = await message.download()
                    await cli.send_photo(
                        chat_id=chatfor.id,
                        photo=path,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                    )
                    os.remove(path)

                elif message.video:
                    path = await message.download()
                    await cli.send_video(
                        chat_id=chatfor.id,
                        video=path,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        duration=message.video.duration,
                        width=message.video.width,
                        height=message.video.height,
                    )
                    os.remove(path)

                elif message.animation:
                    path = await message.download()
                    await cli.send_animation(
                        chat_id=chatfor.id,
                        animation=path,
                        caption=message.caption,
                        caption_entities=message.caption_entities,
                        duration=message.animation.duration,
                        width=message.animation.width,
                        height=message.animation.height,
                    )
                    os.remove(path)

                elif message.video_note:
                    path = await message.download()
                    await cli.send_video_note(
                        chat_id=chatfor.id,
                        video_note=path,
                        duration=message.video_note.duration,
                        length=message.video_note.length,
                    )
                    os.remove(path)
                else:
                    return
                okmsg += 1
            except Exception as e:
                badmsg += 1
                self.ui.forward_log.appendPlainText("Error : [{}]".format(e))
            self.ui.success_count.display(okmsg)
            self.ui.failed_count.display(badmsg)
            self.ui.total_count.display(ana_count)
            self.ui.forward_log.appendPlainText("[{}] {}".format(ana_count, message.id))

        poll_seconds = 5
        self.ui.forward_log.appendPlainText("Watching {} for new photo/video messages every {}s, since {}...".format(fromchat, poll_seconds, activated_at))

        try:
            while Extract:
                try:
                    new_messages = []
                    async for m in cli.get_chat_history(chat_id=chat.id, limit=100):
                        if m.id <= last_id:
                            break
                        new_messages.append(m)
                    if new_messages:
                        last_id = new_messages[0].id
                        for m in reversed(new_messages):
                            if not Extract:
                                break
                            await process_message(m)
                except errors.FloodWait as e:
                    self.ui.forward_log.appendPlainText("FloodWait: {}".format(e.value))
                    await asyncio.sleep(e.value)
                    continue
                except Exception as e:
                    self.ui.forward_log.appendPlainText("Error: {}".format(e))
                await asyncio.sleep(poll_seconds)
        finally:
            self.ui.lbl_status.setText("Status: Disactive")
            await cli.disconnect()
            self.ui.forward_log.appendPlainText("Disconnected from {}.".format(phone))
            self.ui.forward_log.appendPlainText("Extracted {} Massages.".format(ana_count))
            await self.show_async_message("Success", "Extracted {} Massages.".format(ana_count), icon=QMessageBox.Icon.Information)
        return
    
    def safe_filename(slef, name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        name = name.strip().replace(' ', '_')
        return name or 'file'

                
if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow()
    window.show()
    with loop:
        loop.run_forever()
