use tauri::menu::{Menu, MenuItem};
use tauri::{AppHandle, Manager, Wry};

/// Build the system tray menu
pub fn build_tray_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    let show = MenuItem::with_id(app, "show", "Show Dashboard", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, "hide", "Hide Dashboard", true, None::<&str>)?;
    
    // Recording controls
    let start_recording = MenuItem::with_id(app, "start_recording", "Start Recording", true, None::<&str>)?;
    let stop_recording = MenuItem::with_id(app, "stop_recording", "Stop Recording", true, None::<&str>)?;
    
    // Server controls
    let start_server = MenuItem::with_id(app, "start_server", "Start Server", true, None::<&str>)?;
    let stop_server = MenuItem::with_id(app, "stop_server", "Stop Server", true, None::<&str>)?;
    let restart_server = MenuItem::with_id(app, "restart_server", "Restart Server", true, None::<&str>)?;
    let tail_logs = MenuItem::with_id(app, "tail_logs", "Tail Logs", true, None::<&str>)?;
    
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;

    let menu = Menu::new(app)?;
    menu.append(&show)?;
    menu.append(&hide)?;
    menu.append(&tauri::menu::PredefinedMenuItem::separator(app)?)?;
    menu.append(&start_recording)?;
    menu.append(&stop_recording)?;
    menu.append(&tauri::menu::PredefinedMenuItem::separator(app)?)?;
    menu.append(&start_server)?;
    menu.append(&stop_server)?;
    menu.append(&restart_server)?;
    menu.append(&tauri::menu::PredefinedMenuItem::separator(app)?)?;
    menu.append(&tail_logs)?;
    menu.append(&tauri::menu::PredefinedMenuItem::separator(app)?)?;
    menu.append(&quit)?;
    Ok(menu)
}

/// Handle system tray events
pub fn handle_tray_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    match event.id.as_ref() {
        "show" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        "hide" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.hide();
            }
        }
        "start_recording" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let context = app_clone.state::<crate::AppContext>();
                let pm = context.process_manager.lock().await;
                let _ = pm.start_autotype_client(&context.state).await;
            });
        }
        "stop_recording" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let context = app_clone.state::<crate::AppContext>();
                let pm = context.process_manager.lock().await;
                let _ = pm.stop_autotype_client(&context.state).await;
            });
        }
        "start_server" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let context = app_clone.state::<crate::AppContext>();
                let pm = context.process_manager.lock().await;
                let _ = pm.start_whisper_server(&context.state).await;
            });
        }
        "stop_server" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let context = app_clone.state::<crate::AppContext>();
                let pm = context.process_manager.lock().await;
                let _ = pm.stop_whisper_server(&context.state).await;
            });
        }
        "restart_server" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let context = app_clone.state::<crate::AppContext>();
                let pm = context.process_manager.lock().await;
                let _ = pm.restart_whisper_server(&context.state).await;
            });
        }
        "tail_logs" => {
            let app_clone = app.clone();
            tauri::async_runtime::spawn(async move {
                let _ = app_clone.get_webview_window("main").unwrap().eval("window.tauri.invoke('tail_logs')");
            });
        }
        "quit" => {
            app.exit(0);
        }
        _ => {}
    }
}

