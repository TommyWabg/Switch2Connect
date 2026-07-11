# <img width="30" height="30" alt="icon" src="https://github.com/user-attachments/assets/a2b3be7e-851d-4c55-8803-3de00a85eaeb" /> Switch 2 Connect

The ultimate app to connect your Switch 2 Joy-Cons, Switch 2 Pro Controller, and NSO GameCube Controller with standard Bluetooth or ESP32-S3 N16R8 and seamlessly integrate them into the Windows gaming ecosystem. 

## Quick Start

1. Download the `.exe` from the **[Releases](https://github.com/TommyWabg/Switch2Connect/releases)** page.
2. Launch the app. If the WinUHid driver is not installed, a dialog will ask to install it. If you select the USBIP driver mode in the settings, a dialog will ask to install the USBIP driver. Click **Yes** and approve the administrator UAC prompt.
3. Once the installation completes, the setup window will close automatically and the main application will launch.
4. Turn on your Switch 2 controller by holding the Sync button (or pressing any button if already paired). **Do not** pair controllers manually in Windows Bluetooth settings; the app uses automatic GATT discovery.
5. Use the settings panel at the bottom of the app to configure your preferred driver (WinUHid / ViGEmBus / USBIP) and controller layout, gyro sensitivity, and custom button mappings.

## Feature Descriptions

* **Windows 10 Native Compatibility:** Resolved the `AttributeError: property is not available...` crash. Runs flawlessly on Windows 10 (22H2 and above). Windows 11 is still recommanded for 70Hz max bluetooth polling rate, while only 20Hz max on Windows 10 due to the lack of OS driver support for BLE protocol.
* **Low Latency Bluetooth Mode:** The application now forces Windows Bluetooth LE into `ThroughputOptimized` mode upon connection. This drastically drops the connection interval, massively reducing native Bluetooth input delay across the board.
* **Dynamic Emu Mode Toggle:** You can now instantly switch between Xbox One, PS4 (DualShock 4), and PS5 (DualSense) emulation modes directly from the settings panel. This allows you to choose the best protocol for your specific game or platform without restarting the app.
* **Native Motion Support (PS4/PS5 Mode):** Switching to PS4 or PS5 mode enables native motion sensor reporting via the DS4 or DualSense protocol. This provides enhanced compatibility for Steam Input and games that support native DualShock 4 or DualSense gyro features.
* **On-the-Fly Layout Switching:** No more multiple executables! Instantly toggle between **Nintendo Layout** (matching physical labels) and **Xbox Layout** (standard PC positioning) directly from the UI.
* **1000Hz Interpolation:** 1000Hz interpolation loop for ultra-smooth, jitter-free gyro motion rendering with both Switch 2 Right Joy-con and Pro Controller. **Gyro Mouse** and **Joy-con Mouse**now output smoother and lag-free movement at 1000Hz. Gyro data handed off to other external applications (such as third-party emulators) is transmitted at a consistent, high-frequency 1000Hz rate. This transmission is purely non-interpolated; rather than generating synthetic intermediate frames which could introduce latency, the app simply increases the packet delivery rate of real-time physical updates to ensure maximum accuracy and zero artificial delay.
* **Gyro Racing Wheel Mode (Steering):** Reads the controller's absolute tilt (accelerometer) and maps it directly to the Left Analog Stick's X-axis.
* **9-Axis Mouse Mode (Magnetometer Support):** 9-axis motion controled mouse by leveraging the controllers' magnetometer. This provides absolute orientation tracking and eliminates long-term yaw drift. 
* **6-Axis Mouse Mode:** Play shooters or navigate through UI with high-polling rate gyro mouse control. RT and LT act as left and right mouse click when gyro mouse is activated. This mode self-levels horizontal and vertical input regardless of controller tilt.
* **Stick Assist:** Allowing the right thumbstick to work alongside gyro aiming.
* **Gyro Data Passthrough:**
  * **9-Axis Assist:** Integrated the 9-axis IMU fusion bias correction directly into the raw sensor reading pipeline. Using the magnetometer to continuously correct yaw drift for passthrough gyro data.
  * **Horizon Lock:** Added a toggle switch to apply horizon lock to passthrough gyro data. When enabled, it applies roll compensation and maintains the horizontal level. It disables roll data passthrough in this mode, eliminating off-axis cursor drift, roll crosstalk, and gimbal lock.
  * **Adjustable Soft Deadzone Slider:** Added a slider for adjusting soft deadzone value for passthrough gyro data. (Soft deadzone subtracts the active deadzone value from the input magnitude. Output begins smoothly from `0.0` right at the threshold boundary, eliminating step-jump discontinuities.)
* **Gyro Calibration:** **Calibrate Gyro** button for calculate and permanently save sensor bias, eliminating gyro drift.
* **Magnetometer Calibration:** **Calibrate Mag** button for 9-axis accuracy. Perform a "figure-8" motion to calibrate the magnetometer (with a [quick link](https://youtu.be/J_cZnPcW-Yw?si=QWSizI49NQ_5OkA7) to a video tutorial).
* **Custom Extra Button Remapping:** Fully remap extra buttons like `GL`, `GR`, `SL_R`, `SR_L` and `Chat` to function as gyro trigger, DualShock/DualSense trackpad click, calibration trigger, or standard buttons.
* **Joy-con Mouse Toggle:** A new dedicated switch in the GUI to enable or disable the Joy-con mouse mode. This prevents accidental cursor movement during gameplay.
* **Dynamic Split & Merge System:** The new **Split** and **Merge** features allow you to detach combined Joy-cons into two individual controllers or combine single Joy-cons into one unified virtual gamepad without restarting.
* **Vertical & Horizontal Hold Modes Switch (V/H):** Added V/H switch buttons, allowing users to toggle between Vertical (standard upright) and Horizontal (sideways) hold modes for single Joy-cons.
* **Per-Joy-Con V/H Mode Persistence:** The application now records and remembers whether each single Joy-Con is held vertically or horizontally. Layout preferences (Vertical or Horizontal) are dynamically mapped to each controller's Bluetooth MAC address and saved in `config.yaml`.
* **Dual-Controller Gyro Selection (L/R Gyro):** When using a pair of Joy-cons as a single virtual controller, you can now manually select which Joy-con (Left or Right) provides the motion data. This allows for greater flexibility, letting you choose your preferred hand for gyro aiming or motion controls.
* **Interactive Controller Identification:** Added a dedicated **Vibrate** button for each player slot. This allows for instant physical feedback, helping you quickly identify which Joy-con belongs to which player in a multiplayer setup.
* **Haptic & OS Integration:** Added rumble feedback (including a connection confirmation rumble) and mapped the Capture button to native Windows screenshots (`Win + PrtScn`).
* **One-Click Disconnect:** Added a convenient 'X' button to the top right of each connected controller's UI block. You can now manually disconnect specific controllers directly from the interface without needing to power them off physically.
* **Dedicated UI Driver Controls:** Added an **Install/Uninstall WinUHid Driver** button to the left of the "Run At Startup" button.
* **Run at Startup:** Added a toggle to automatically launch the application with Windows.
* **Start Minimized:** Option to launch directly to the system tray for a seamless background experience.
* **Hide to system tray:** Added the ability to minimize the application to the Windows system tray.
* **Standalone Executable (.exe):** Fully packed with all dependencies (including vgamepad DLLs). No Python installation required.

## System Requirements

* **Operating System:** Windows 10 (22H2 or above) or Windows 11.
    * *Note:* **Windows 11 is highly recommended** for the best experience. It supports a maximum Bluetooth LE polling rate of **70Hz**, while Windows 10 is limited to **20Hz** due to the lack of OS driver support for the BLE protocol.
* **Bluetooth Hardware:** Bluetooth 5.0 or above is required for stable connectivity and low-latency performance.
* **Driver:** [lurebat's WinUHid driver](https://github.com/lurebat/WinUHid) is required for virtual gamepad emulation (supporting Xbox, PS4, and PS5/DualSense emulation). For the Switch2 emulation mode, the open-source [usbip-win2 driver](https://github.com/czgdp1807/usbip-win2) is required and used.
    * *Auto-Installation:* The app will automatically detect if the selected driver (WinUHid or USBIP) is missing and guide you through a one-click installation (requires administrator privileges).

## Important Setting for Steam Users:
Because this app emulates both Xbox One and PS4/PS5 controllers, Steam Input might try to "help" by applying its own layout overrides, which can double-swap your buttons and mess up your in-game controls! 
**To ensure your layout stays consistent:**
1. Go to **Steam** > **Settings** > **Controller** > **Show Advanced Settings**.
2. Make sure "**Enable Steam Input for Xbox controllers**" is turned **ON**.
3. Make sure "PlayStation Controller Support" is set to **Enabled**. (**NOT** Enabled in Games w/o Supports)
4. Now the Switch2Connect app will handle the layout switching for you!

## Gyro Calibration Guide

To ensure maximum precision and eliminate "cursor drift," follow these steps to calibrate 6-axis gyro:
1.  **Stationary Placement:** Place your Pro Controller on a completely flat, stable surface. **Do not touch or move it during the process.**
2.  **Trigger Calibration:** Click the **[Calibrate Gyro]** button in the settings panel.
3.  **Wait for Countdown:** The UI will display a countdown (`Calibrating (2..)`). 
4.  **Completion:** Once the button displays `Calibration Done`, the software has calculated the hardware bias and saved it. You do not need to recalibrate unless you experience new drifting issues.

## Mag Calibration Guide

To achieve drift-free 9-axis tracking, follow these steps to calibrate the magnetometer:
1.  **Trigger Calibration:** Click the **[Calibrate Mag]** button in the settings panel. The button will turn orange and display `Stop Mag Calib`.
2.  **Figure-8 Motion:** Hold the controller and move it continuously in a **"figure-8"** pattern in the air. Ensure you rotate the controller across all three axes to capture the full magnetic field range.
3.  **Reference Video:** If you are unsure of the motion, click the [**'figure 8'** link](https://youtu.be/J_cZnPcW-Yw?si=QWSizI49NQ_5OkA7) in the UI to watch a short demonstration video.
4.  **Save & Finish:** After performing the motion for about 5-10 seconds, click the **[Stop Mag Calib]** button to save the calibration data. The software will now use the new magnetic bias for stabilized orientation.

## Support This Project

Built with a user-experience-first mindset, this project has reached a mature and comprehensive state and will remain completely free to use forever.

If you are highly satisfied when gaming with this app, please consider leaving a tip. Any financial "thank you" is incredibly appreciated!

<div align="left">
  <a href="https://ko-fi.com/tagayama">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="ko-fi">
  </a>
</div>

Happy gaming, and thank you for your generous support!

##
**This project is developed based on and has been extensively modified from [Nadeflore/switch2-controllers](https://github.com/Nadeflore/switch2-controllers). I would like to thank the original author for her foundational work.**
