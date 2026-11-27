import discordrpc
from discordrpc.button import Button
import threading

# Глобальная переменная для остановки потока
stop_event = threading.Event()

button = Button(
  button_one_label="Repository",
  button_one_url="https://github.com/JErfurt/focuswithai",
  button_two_label="Example",
  button_two_url="https://www.youtube.com/watch?v=AqjlrRfCPb8"
  )

# Функция для запуска Discord RPC
def start_rpc(app_id, state, details, started_at, img, stopped):
    connection = None
    try:
        if stopped.is_set():
            return
        connection = discordrpc.RPC(app_id=app_id, exit_if_discord_close=False)
        if not stopped.is_set():
            connection.set_activity(
                state=state, details=details, large_image=img, ts_start=started_at, buttons=button,
                large_text="https://github.com/JErfurt/focuswithai",
            )
            stopped.wait()
    except Exception as error:
        print(f"[Discord] {error}")
    finally:
        if connection:
            connection.disconnect()


def stop_rpc():
    stop_event.set()


def restart_rpc(app_id, state, details, started_at, img):
    global stop_event
    stop_rpc()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=start_rpc, args=(app_id, state, details, started_at, img, stop_event), daemon=True,
    )
    thread.start()
    return thread
