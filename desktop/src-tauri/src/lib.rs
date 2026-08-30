use std::sync::Mutex;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, AppHandle, Emitter};
use tauri::tray::{TrayIconBuilder, MouseButton, MouseButtonState, TrayIconEvent};
use tauri::menu::{Menu, MenuItem};

struct BackendState {
    port: Mutex<Option<u16>>,
    process: Mutex<Option<u32>>,
}

#[tauri::command]
fn get_backend_port(state: tauri::State<BackendState>) -> Option<u16> {
    *state.port.lock().unwrap()
}

#[tauri::command]
fn set_backend_port(port: u16, state: tauri::State<BackendState>) {
    *state.port.lock().unwrap() = Some(port);
}

#[tauri::command]
fn set_backend_process(pid: u32, state: tauri::State<BackendState>) {
    *state.process.lock().unwrap() = Some(pid);
}

#[tauri::command]
fn get_backend_process(state: tauri::State<BackendState>) -> Option<u32> {
    *state.process.lock().unwrap()
}

#[tauri::command]
fn show_output_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("output") {
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn hide_output_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("output") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn toggle_output_fullscreen(app: AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("output") {
        let is_fs = window.is_fullscreen().map_err(|e| e.to_string())?;
        window.set_fullscreen(!is_fs).map_err(|e| e.to_string())?;
        return Ok(!is_fs);
    }
    Ok(false)
}

#[tauri::command]
fn get_output_fullscreen(app: AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("output") {
        return window.is_fullscreen().map_err(|e| e.to_string());
    }
    Ok(false)
}

#[tauri::command]
async fn restart_backend(app: AppHandle) -> Result<(), String> {
    let state: tauri::State<BackendState> = app.state();
    // Kill existing process
    if let Some(pid) = *state.process.lock().unwrap() {
        #[cfg(target_os = "windows")]
        {
            use std::process::Command;
            let _ = Command::new("taskkill").args(["/F", "/PID", &pid.to_string()]).output();
        }
        #[cfg(not(target_os = "windows"))]
        {
            unsafe { libc::kill(pid as i32, libc::SIGTERM); }
        }
    }
    // The sidecar will be re-launched by the frontend after clearing state
    *state.port.lock().unwrap() = None;
    *state.process.lock().unwrap() = None;
    app.emit("backend-restarted", ()).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .manage(BackendState {
            port: Mutex::new(None),
            process: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            get_backend_port,
            set_backend_port,
            set_backend_process,
            get_backend_process,
            show_output_window,
            hide_output_window,
            toggle_output_fullscreen,
            get_output_fullscreen,
            restart_backend,
        ])
        .setup(|app| {
            // System tray
            let show_mi = MenuItem::with_id(app, "show", "Show FaithView", true, None::<&str>)?;
            let output_mi = MenuItem::with_id(app, "output", "Show Output", true, None::<&str>)?;
            let restart_mi = MenuItem::with_id(app, "restart", "Restart Engine", true, None::<&str>)?;
            let quit_mi = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_mi, &output_mi, &restart_mi, &quit_mi])?;
            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("FaithView Pro")
                .on_menu_event(move |app, event| {
                    match event.id.as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "output" => {
                            if let Some(w) = app.get_webview_window("output") {
                                let _ = w.show();
                            }
                        }
                        "restart" => {
                            app.emit("restart-backend", ()).unwrap();
                        }
                        "quit" => {
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Show main window
            if let Some(w) = app.get_webview_window("main") {
                w.show()?;
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
