# switch2-controllers (Windows 10 & Pro Features Fork)

This fork is heavily optimized for Windows 10/11 users, featuring a fully interactive GUI, advanced Gyro mouse aiming, and on-the-fly layout switching. 

## Key Features

* **Windows 10 Native Compatibility:** Resolved the `AttributeError: property is not available...` crash. Runs flawlessly on Windows 10 (22H2 and above). Windows 11 is still recommanded for 70Hz max bluetooth polling rate, while only 20Hz max on Windows 10 due to the lack of OS driver support for BLE protocol.
* **Low Latency Bluetooth Mode:** The application now forces Windows Bluetooth LE into `ThroughputOptimized` mode upon connection. This drastically drops the connection interval, massively reducing native Bluetooth input delay across the board.
* **Flexible Multi-Driver & Emulation Backend (WinUHid, ViGEmBus & USBIP):** Upgraded the emulation backend to support a flexible multi-driver architecture. Users can now seamlessly toggle between the built-in [WinUHid](https://github.com/lurebat/WinUHid) driver, the industry-standard [ViGEmBus](https://github.com/nefarius/ViGEmBus) virtual gamepad emulator, and a [USBIP](https://github.com/vadimgrn/usbip-win2)-based emulation mode which enables emulating a physical USB-connected Switch 2 Pro Controller.
  * *A special thank you to [LeonChrome](https://github.com/LeonChrome) for [proactively reaching out](https://www.reddit.com/r/NintendoSwitch2/comments/1t4yx0x/comment/ons2veu/?utm_source=share&utm_medium=web3x&utm_name=web3xcss&utm_term=1&utm_content=share_button) and sharing their open-source project [y700-switch2-pro-bridge](https://github.com/LeonChrome/y700-switch2-pro-bridge), which served as a crucial reference for successfully building this Switch 2 emulation mode.*
* **Dynamic Emu Mode Toggle:** You can now instantly switch between Xbox 360, Xbox One, PS4 (DualShock 4), PS5 (DualSense), Switch 2 Pro Controller, and Switch 1 Joy-Con/Pro Controller emulation modes directly from the settings panel. This allows you to choose the best protocol for your specific game or platform without restarting the app.
* **Native Motion Support (PS4/PS5/Switch1/Switch2 Mode):** Switching to PS4, PS5, Switch1, or Switch2 mode enables native motion sensor reporting. This provides enhanced compatibility for Steam Input, 3rd party platforms, and games that support native DualShock 4, DualSense, or Switch 1/2 gyro features.
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
* **Dual Joy-con Gyro (DJG):** Introduced a gyro fusion system that combines motion data from both Left and Right Joy-cons when used as a merged pair. This system designates a "Dominant" side for spatial orientation and uses the "Sub" side as an accelerator for larger movements. A magnitude threshold of 30 is applied to the sub side. It contributes to acceleration only when the dominant side exceeds this threshold. Sub side acceleration is strictly capped to a maximum of 2x the dominant movement.
  * Navigate to the **Dual Joy-con Gyro (DJG)** panel.
  * Click the **DJG** toggle to **ON** to enable the fusion engine.
  * Set the **Dominant Side** to **Left** or **Right**. The dominant side acts as the primary reference for direction and gravity, while the sub side provides acceleration.
* **DJG Trigger Mapping:** Added a dedicated "DJG" option to the Extra Button Mapping settings.
  * Assign the "DJG" action to any available extra button to serve as the hardware trigger for DJG features.
  * Pressing this mapped button during gameplay will execute the action defined by the current DJG Control Mode and DJG Activation settings.
* **DJG Control Modes:** Three modes to dictate how the mapped DJG trigger button behaves during gameplay.
  * **Single Side Toggle:** Toggle the gyro tracking of a single Joy-con on or off independently.
  * **Switch Dominant Side:** Swap the Dominant and Sub roles between the Left and Right Joy-cons. Both sides are forced active upon switching.
  * **Switch Gyro Side:** Turn off the current gyro and activate the opposite Joy-con's gyro exclusively. The Dominant Side setting syncs automatically.
* **DJG Activation Types:** Trigger behavior options to support different input styles.
  * **Toggle:** Switch the DJG state once per button press.
  * **Hold:** Switch the DJG state when the button is pressed, and revert to the original state when the button is released.
* **Custom Extra Button Remapping:** Fully remap extra buttons like `GL`, `GR`, `SL_L`, `SL_R`, `SR_L`, `SR_R`, `Home`, `Capture`, and `Chat` to function as gyro trigger, DualShock/DualSense trackpad click, calibration trigger, dual Joy-con gyro trigger, print screen, change profile, system manager, standard buttons, or record custom input.
* **Advanced Custom Input Remapping:** Introduced a powerful new "Custom" mapping feature. Users can now record and assign any complex combination of keyboard keys, mouse clicks, or controller buttons to a single input. This flexible system supports both "Tap" (fires the recorded sequence momentarily) and "Hold" (sustains the sequence for as long as the button is pressed) modes.
  * Click the dropdown menu and select the "Custom" option.
  * Press and hold your desired combination of keyboard keys, mouse clicks, and/or controller buttons simultaneously.
  * Release all inputs. The recording will automatically stop and save your sequence.
  * Click the adjacent toggle button to switch between **Tap** (triggers the sequence once) and **Hold** (keeps the sequence pressed as long as you hold the controller button).
  * Click the **X** button to remove custom input and fall back to the default.
* **Categorized Button Mapping Profiles:** Segregated custom button mapping and rumble configurations into three independent target categories: **Xbox**, **PS4**, **PS5**, and **Switch 2**. Remap profiles are now saved and loaded automatically based on the active emulation mode.
* **Custom Mapping Profile System:** Implemented a profile management system. Users can create, rename, delete, and switch between multiple configurations. Each profile persistently stores button mappings, emulation mode, and driver settings.
* **Joy-con Mouse Toggle:** A new dedicated switch in the GUI to enable or disable the Joy-con mouse mode. This prevents accidental cursor movement during gameplay.
* **Dynamic Split & Merge System:** The new **Split** and **Merge** features allow you to detach combined Joy-cons into two individual controllers or combine single Joy-cons into one unified virtual gamepad without restarting.
* **Vertical & Horizontal Hold Modes Switch (V/H):** Added V/H switch buttons, allowing users to toggle between Vertical (standard upright) and Horizontal (sideways) hold modes for single Joy-cons.
* **Per-Joy-Con V/H Mode Persistence:** The application now records and remembers whether each single Joy-Con is held vertically or horizontally. Layout preferences (Vertical or Horizontal) are dynamically mapped to each controller's Bluetooth MAC address and saved in `config.yaml`.
* **Dual-Controller Gyro Selection (L/R Gyro):** When using a pair of Joy-cons as a single virtual controller, you can now manually select which Joy-con (Left or Right) provides the motion data. This allows for greater flexibility, letting you choose your preferred hand for gyro aiming or motion controls.
* **Customizable Rumble Strength:** Introduced a new Vibration Strength slider in the settings panel (ranging from 0 to 10), allowing users to dynamically scale the intensity of the controller's haptic feedback.
* **Rumble Frequency Slider:** You can now customize the vibration/rumble frequency directly from the settings panel.
* **Dual Rumble Mode Toggle:** Introduced a toggle switch in the user interface to easily switch between Xbox and Switch rumble modes. 
  * **Xbox Mode:** Tailored for standard PC games to simulate dual-motor rumble by activating dynamic frequency scaling and high-frequency masking to mimic traditional gamepad motors. 
    * **Strength 5 and Frequency 10** emulates the feel of a DualSense Edge controller.
    * **Strength 10 and Frequency 10** emulates the rumble of an Xbox Elite Series 1 Controller.
  * **Switch Mode:** Mimics the native Switch HD Rumble (LRA) experience. It bypasses custom frequency scaling and masking, routing raw frequency data directly to the controller for a tighter, softer, and more detailed tactile feedback. Best suited for native Nintendo game emulations.
* **Interactive Controller Identification:** Added a dedicated **Ping** button for each player slot. This allows for instant physical feedback, helping you quickly identify which Joy-con belongs to which player in a multiplayer setup.
* **Haptic & OS Integration:** Added rumble feedback (including a connection confirmation rumble) and mapped the Capture button to native Windows screenshots (`Win + PrtScn`).
* **One-Click Disconnect:** Added a convenient 'X' button to the top right of each connected controller's UI block. You can now manually disconnect specific controllers directly from the interface without needing to power them off physically.
* **Auto-Disconnect Options:** Added a 3-way Auto-Disconnect toggle (OFF, Inactive, Absolute).
  * **Inactive:** tracks physical button and stick inputs to automatically disconnect idle controllers while keeping active players connected.
  * **Absolute:** tracks the overall time each controller is connected to the app and disconnects the ones that reach the time limit.
* **Dedicated UI Driver Controls:** Added **Install/Uninstall Driver** buttons to the left of the "Run At Startup" button.
* **Run at Startup:** Added a toggle to automatically launch the application with Windows.
* **Start Minimized:** Option to launch directly to the system tray for a seamless background experience.
* **Hide to system tray:** Added the ability to minimize the application to the Windows system tray.
* **Standalone Executable (.exe):** Fully packed with all dependencies (including vgamepad DLLs). No Python installation required.

## System Requirements

* **Operating System:** Windows 10 (22H2 or above) or Windows 11.
    * *Note:* **Windows 11 is highly recommended** for the best experience. It supports a maximum Bluetooth LE polling rate of **70Hz**, while Windows 10 is limited to **20Hz** due to the lack of OS driver support for the BLE protocol.
* **Bluetooth Hardware:** Bluetooth 5.0 or above is required for stable connectivity and low-latency performance.
* **Driver:** [lurebat's WinUHid driver](https://github.com/lurebat/WinUHid) is required for virtual gamepad emulation (supporting Xbox, PS4, and PS5/DualSense emulation).
    * *Auto-Installation:* The app will automatically detect if the driver is missing on first launch and guide you through a one-click installation (requires administrator privileges).

## Quick Start

1. Download the `.exe` from the **[Releases](https://github.com/TommyWabg/switch2-controllers-windows10-gyro/releases)** page.
2. Launch the app. If the driver is not installed, a dialog will ask to install the WinUHid driver. Click **Yes** and approve the administrator UAC prompt.
3. Once the installation completes, the setup window will close automatically, and the main application will launch.
4. Turn on your Switch 2 controller by holding the Sync button (or pressing any button if already paired). **Do not** pair controllers manually in Windows Bluetooth settings; the app uses automatic GATT discovery.
5. Use the settings panel at the bottom of the app to configure your preferred controller layout (Xbox / PS4 / PS5), gyro sensitivity, and custom button mappings.

## Important Setting for Steam Users:
Because this app emulates both Xbox One and PS4/PS5 controllers, Steam Input might try to "help" by applying its own layout overrides, which can double-swap your buttons and mess up your in-game controls! 
**To ensure your layout stays consistent:**
1. Go to **Steam** > **Settings** > **Controller** > **Show Advanced Settings**.
2. Make sure "**Enable Steam Input for Xbox controllers**" is turned **ON**.
3. Make sure "PlayStation Controller Support" is set to **Enabled**. (**NOT** Enabled in Games w/o Supports)
4. Now the Switch_2_Controllers app will handle the layout switching for you!

## Gyro Calibration Guide

To ensure maximum precision and eliminate "cursor drift," follow these steps to calibrate 6-axis gyro:
1.  **Stationary Placement:** Place your controllers on a completely flat, stable surface. **Do not touch or move it during the process.**
2.  **Trigger Calibration:** Click the **[Calibrate Gyro]** button in the settings panel.
3.  **Wait for Countdown:** The UI will display a countdown (`Calibrating (2..)`). 
4.  **Completion:** Once the button displays `Calibration Done`, the software has calculated the hardware bias and saved it. You do not need to recalibrate unless you experience new drifting issues.

## Mag Calibration Guide

To achieve drift-free 9-axis tracking, follow these steps to calibrate the magnetometer:
1.  **Trigger Calibration:** Click the **[Calibrate Mag]** button in the settings panel. The button will turn orange and display `Stop Mag Calib`.
2.  **Figure-8 Motion:** Hold the controller and move it continuously in a **"figure-8"** pattern in the air. Ensure you rotate the controller across all three axes to capture the full magnetic field range.
3.  **Reference Video:** If you are unsure of the motion, click the [**'figure 8'** link](https://youtu.be/J_cZnPcW-Yw?si=QWSizI49NQ_5OkA7) in the UI to watch a short demonstration video.
4.  **Save & Finish:** After performing the motion for about 5-10 seconds, click the **[Stop Mag Calib]** button to save the calibration data. The software will now use the new magnetic bias for stabilized orientation.

##
**This project is developed based on and has been extensively modified from [Nadeflore/switch2-controllers](https://github.com/Nadeflore/switch2-controllers). I would like to thank the original author for her foundational work.**

---
*(Below is the original project description)*

# switch2 controllers
An app to use switch 2 joycons on pc as gamepad and mouse

### Usage

No need to pair the controller in the bluetooth settings.

Simply launch the app, and do what it says.

If you already paired the joycons in windows bluetooth settings, remove it before attempting to use it with this app.

### Using as a mouse

By default the app switches a joycon to mouse mode when it detects it's being used a mouse (side of of the joycon against a flat surface)

When in mouse mode, the following buttons are used as mouse buttons and no longer useable as gamepad buttons :
L/R : left click
ZL/ZR : right click
joystick : mouse wheel and middle button (click)

If you do not wish to use mouse mode, you can disable it in the config

### Using joycons sideways

By default, the app will always try to combine a right and left joycons together to make a single virtual controller.

If you wish to use both joycons sideway, you can hold SL\SR while turning them on
An other option is to set `combine_joycons` in the config to false so that the app will never try to combine joycons
