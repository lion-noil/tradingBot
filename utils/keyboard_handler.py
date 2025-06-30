from pynput import keyboard

def start_keyboard_listener(queue):
    def on_press(key):
        if key == keyboard.Key.up:
            queue.put("buy")
        elif key == keyboard.Key.down:
            queue.put("sell")
        elif key == keyboard.Key.right:
            queue.put("close")

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()
