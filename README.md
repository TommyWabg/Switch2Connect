# Switch 2 Connect
### The ultimate app to connect your Switch 2 Joy-Cons, Switch 2 Pro Controller, and NSO GameCube Controller with standard Bluetooth or ESP32-S3 N16R8 and seamlessly integrate them into the Windows gaming ecosystem.
<p align="center">
 <a href="https://github.com/TommyWabg/Switch2Connect/releases/latest/download/Switch2Connect_v1.5.exe"><img width="200" alt="icon" src="https://github.com/TommyWabg/Switch2Connect/blob/main/resources/images/icon.png" />
</p>
<p align="center">
  <a href="https://github.com/TommyWabg/Switch2Connect/releases"><img src="https://img.shields.io/github/v/release/TommyWabg/Switch2Connect?style=flat-square&color=9be1e6&labelColor=e4896e" alt="Release version"></a>
  <a href="https://github.com/TommyWabg/Switch2Connect/releases/latest/download/Switch2Connect_v1.5.exe"><img src="https://img.shields.io/github/downloads/TommyWabg/Switch2Connect/total.svg?style=flat-square&color=9be1e6&labelColor=e4896e" alt="Contributors"></a>
  <a href="https://github.com/TommyWabg/Switch2Connect/blob/main/LICENSE.md"><img src="https://img.shields.io/github/license/TommyWabg/Switch2Connect?style=flat-square&color=9be1e6&labelColor=e4896e" alt="License"></a>
  <br>
  <a href="https://github.com/TommyWabg/Switch2Connect#system-requirements"><img src="https://img.shields.io/badge/platform-Windows%2010/11%20App%20%7C%20ESP32--S3%20N16R8%20Firmware-287cff?style=flat-square&color=9be1e6&labelColor=e4896e" alt="Platform: Windows 10 & 11 app and ESP32-S3 R16N8 firmware">
</p>
<p align="center">
  <a href="https://ko-fi.com/tagayama">
    <img width="200" src="https://storage.ko-fi.com/cdn/brandasset/v2/support_me_on_kofi_beige.png?_gl=1*1ndc5yc*_gcl_au*MTAzOTY3NTA1Ni4xNzgyODE2Nzkx*_ga*MTQ5NTE2MDk5MC4xNzgyODE2Nzky*_ga_M13FZ7VQ2C*czE3ODQzMDg3NDckbzQ0JGcxJHQxNzg0MzA5NTQxJGo1OSRsMCRoMA.." alt="ko-fi">
  </a>
</p>

## Quick Start

1. Download the latest version of **[Switch2Connect.exe](https://github.com/TommyWabg/Switch2Connect/releases/latest/download/Switch2Connect_v1.5.exe)**.
2. Launch the app. If the WinUHid driver is not installed, a dialog will ask to install it. If you select the USBIP driver mode in the settings, a dialog will ask to install the USBIP driver. Click **Yes** and approve the administrator UAC prompt.
3. Once the installation completes, the setup window will close automatically, and the main application will launch.
4. Turn on your Switch 2 controller by holding the Sync button (or pressing any button if already paired). **Do not** pair controllers manually in Windows Bluetooth settings; the app uses automatic GATT discovery.
5. Use the settings panel at the bottom of the app to configure your preferred driver (WinUHid / ViGEmBus / USBIP) and controller layout, gyro sensitivity, and custom button mappings.

## Feature Descriptions

* **Windows 10 Native Compatibility:** Supports Windows 10 22H2 and Windows 11. Windows 11 supports Bluetooth LE polling rates up to 70Hz, while Windows 10 is limited to 20Hz by the operating system’s BLE driver implementation.
* **Low Latency Bluetooth Mode:** Configures Windows Bluetooth LE connections with the ThroughputOptimized mode to reduce the connection interval and controller input latency.
* **ESP32-S3 N16R8 Low-Latency Connection:** Supports the ESP32-S3 N16R8 development board as a BLE bridge with polling rates up to 133Hz. The application provides firmware installation and repair, controller pairing through the SYNC button, and reconnection for controllers bonded to the bridge.
* **Wired Pro Controller Support:** Supports wired connected Switch 2 Pro Controller with up to 500Hz polling rate. The **Wired Pro Controller** menu allows users to manage HidHide, toggle automatic discovery, and run a one-time manual scan when needed.
* **Multiple Driver and Emulation Backends:** Supports WinUHid, ViGEmBus, and USBIP backends. WinUHid provides Xbox One, PS4, and PS5 emulation; ViGEmBus provides Xbox 360 and PS4 emulation; USBIP provides USB-connected Switch 2 Pro Controller and DualSense emulation. The application detects missing or incomplete WinUHid and ViGEmBus installations and provides Install, Repair, and Uninstall actions. ViGEmBus can be downloaded and installed from the application after UAC authorization.
* **Tabbed Settings Interface:** Organizes settings into Controller Mapping, Mode Shift Mapping, and Gyro Settings tabs.
* **DualSense Audio Haptic Feedback:** The USBIP-based PS5 emulation mode exposes a four-channel audio playback endpoint for games that support DualSense audio haptics. It supports DualSense audio haptics, standard PS5 rumble, Xbox rumble translation, and adaptive-trigger feedback translated into independent HD Rumble output for the left and right triggers.
* **Xbox One Impulse Trigger Support:** In WinUHid Xbox One emulation mode, Xbox Impulse Trigger force-feedback values from LT and RT are translated into Switch 2 high-frequency HD Rumble output. Standard gamepad rumble is emitted as the same mono signal on both physical sides, while Impulse Trigger feedback is routed as independent left and right high-frequency overlays for trigger-specific effects.
  * **Impulse Trigger Settings:** The **Impulse Trigger Settings** button appears beside **Rumble Mode** in WinUHid Xbox One emulation mode. The floating settings window provides controls for enabling impulse feedback, selecting dynamic or fixed frequency behavior, adjusting impulse strength, and choosing a fixed frequency level.
  * **Dynamic Frequency:** When enabled, incoming impulse strength determines both output frequency and amplitude. During the release envelope, frequency continues to follow the current decaying output strength.
  * **Fixed Frequency:** When Dynamic Frequency is disabled, the **Frequency** slider selects the impulse output frequency independently from incoming impulse strength.
  * **Release Envelope:** When an Impulse Trigger stop command is received, the current output decays linearly to zero over 90 ms. A new command on the same side immediately replaces the pending release.
* **Native Motion Support (PS4/PS5 Mode):** Switching to PS4 or PS5 mode enables native motion sensor reporting via the DS4 or DualSense protocol. This provides enhanced compatibility for Steam Input and games that support native DualShock 4 or DualSense gyro features.
* **Cemuhook UDP Server:** Sends controller motion data to compatible applications through 127.0.0.1:26760. Select Cemuhook in the Gyro Pass-through settings to enable the server.
* **Cemuhook Gyro Sensitivity Adjustment:** Featuring a Sensitivity slider (levels 1-5) to the "Gyro Passthrough" panel. The sensitivity specifically applies a linear multiplier only to the horizontal rotation (Yaw) axis sent via the Cemuhook UDP protocol.
  * **levels 1:** Virtual Switch 1 Joy-cons turn 360 degrees when the physical Switch 2 Joy-cons turn 360 degrees.
  * **levels 5:** Matches real Switch 1 Joycon sensitivity.
* **On-the-Fly Layout Switching:** Toggle between **Nintendo Layout** (matching physical labels) and **Xbox Layout** (standard PC positioning) directly from the UI.
* **NSO GameCube Controller Layout Mapping:** The NSO GameCube Controller uses consistent ABXY layout mapping across Xbox, PlayStation, and Switch emulation modes. Xbox Layout maps buttons by physical position, while Switch Layout maps buttons by input function.
* **1000Hz Interpolation:** 1000Hz interpolation loop for ultra-smooth, jitter-free gyro motion rendering with both Switch 2 Right Joy-con and Pro Controller. **Gyro Mouse** and **Joy-con Mouse** output smoother and lag-free movement at 1000Hz. Gyro data handed off to other external applications (such as third-party emulators) is transmitted at a consistent, high-frequency 1000Hz rate. This transmission is purely non-interpolated; rather than generating synthetic intermediate frames, which could introduce latency, the app simply increases the packet delivery rate of real-time physical updates to ensure maximum accuracy and zero artificial delay.
* **In-app Gyro Control (Mouse, R Joystick & Steering):** Utilize in-app gyro control for mouse aiming, or select R Joystick and Steering options. The Steering mode reads the controller's absolute tilt (accelerometer) and maps it directly to the Left Analog Stick's X-axis. Unlike the Mouse control mode, the R Joystick and Steering modes utilize a separate In-app Gyro mapping scope where all buttons default to their standard controller inputs to prevent unexpected mouse clicks during controller emulation.
* **9-Axis Mouse Mode (Magnetometer Support):** A 9-axis motion-controlled mouse by leveraging the controllers' magnetometer. This provides absolute orientation tracking and eliminates long-term yaw drift. 
* **6-Axis Mouse Mode:** Play shooters or navigate through UI with high-polling rate gyro mouse control. RT and LT act as left and right mouse clicks when the gyro mouse is activated. This mode self-levels horizontal and vertical input regardless of controller tilt.
* **Gyro Racing Wheel Mode (Steering):** Reads the controller's absolute tilt (accelerometer) and maps it directly to the Left Analog Stick's X-axis.
* **Stick Assist:** Allowing the right thumbstick to work alongside gyro aiming.
* **In-App Gyro Trigger Deadzone:** Configurable within the In-App Gyro mapping pop-up window. Assign one or more buttons to apply a dedicated Trigger Deadzone while In-App Gyro is active. Users can customize the deadzone amount, gyro pause duration after button press and release, and how long the deadzone effect remains active after release. This helps reduce unintended gyro movement caused by button presses, releases, or controller vibration.
* **In-App Gyro Trigger Dampening:** Configurable within the In-App Gyro mapping pop-up window. Assign one or more buttons to proportionally reduce gyro sensitivity by a customizable percentage. Users can also customize how long the dampening effect remains active after the assigned button is released, allowing smoother control during aiming, steering, or other gyro-based actions.
* **In-app Gyro Lock:** A dedicated mapping option to pause gyro control while remaining in In-app Gyro mode, supporting both Hold and Tap activation logic.
* **Gyro Pass-through:**
  * **9-Axis Assist:** Uses magnetometer-assisted IMU fusion to correct long-term yaw drift in pass-through motion data.
  * **Horizon Lock:** Applies roll compensation and maintains a horizontal reference while suppressing roll output.
  * **Adjustable Soft Deadzone Sliders:** Provides separate soft-deadzone controls for In-App Gyro and pass-through motion. Output starts from zero at the configured threshold to avoid a step change.
* **Gyro Calibration:** **Calibrate Gyro** button to calculate and permanently save sensor bias, eliminating gyro drift.
* **Magnetometer Calibration:** **Calibrate Mag** button for 9-axis accuracy. Perform a "figure-8" motion to calibrate the magnetometer (with a [quick link](https://youtu.be/J_cZnPcW-Yw?si=QWSizI49NQ_5OkA7) to a video tutorial).
* **Dual Joy-con Gyro (DJG):** Featuring a gyro fusion system that combines motion data from both Left and Right Joy-cons when used as a merged pair for stutter-free aiming when ratcheting. This system designates a "Dominant" side for spatial orientation and uses the "Sub" side as an accelerator for larger movements. A magnitude threshold of 30 is applied to the sub side. It contributes to acceleration only when the dominant side exceeds this threshold. Sub side acceleration is strictly capped to a maximum of 2x the dominant movement, and its opposite directional movement will be ignored. When the dominant side's gyro is turned off, the sub side takes over control.
  * Navigate to the Dual Joy-Con Gyro (DJG) panel.
  * Click the DJG toggle to ON to enable the fusion engine.
  * Set the Dominant Side to Left or Right. The dominant side acts as the primary reference for direction and gravity, while the sub side provides acceleration.
* **DJG Trigger Mapping:** Featuring a dedicated "DJG" option to the Extra Button Mapping settings.
  * Assign the "DJG" action to any available extra button to serve as the hardware trigger for DJG features.
  * Pressing this mapped button during gameplay will execute the action defined by the current DJG Control Mode and DJG Activation settings.
* **DJG Control Modes:** Three modes to dictate how the mapped DJG trigger button behaves during gameplay.
  * **Switch Dominant Side:** Swap the Dominant and Sub roles between the Left and Right Joy-cons. Both sides are forced to be active upon switching.
  * **Switch Gyro Side:** Turn off the current gyro and activate the opposite Joy-Con's gyro exclusively. The Dominant Side setting syncs automatically.
  * **Single Side Toggle:** Toggles the gyro tracking state of one Joy-Con independently. A DJG trigger mapped on the Left Joy-Con controls the L Gyro, while a trigger mapped on the Right Joy-Con controls the R Gyro. Select **Dominant Side: Left** or **Right** to choose the primary side. Select **Dominant Side: None** to combine enabled motion input from both Joy-Cons directly while allowing DJG controls to manage each side.
* **DJG Activation Types:** Trigger behavior options to support different input styles.
  * Toggle: Switch the DJG state once per button press.
  * Hold: Switch the DJG state when the button is pressed, and revert to the original state when the button is released.
* **Per-Controller Joystick Deadzones:** Configure left and right joystick deadzones independently for Pro Controller, Joy-Con, and NSO GameCube Controller. Each controller family stores its own deadzone values, with an option to link or separate the left and right joystick settings.
* **Full Controller Remappability:** All buttons (including extra buttons like GL, GR, SL_L, SL_R, SR_L, SR_R, NSO GCN L/R Analog Trigger Click, Home, Capture, and Chat) can be fully remapped to Switch inputs, PlayStation inputs, In-app controls, Windows controls, mouse clicks, or recorded custom input. Joysticks can also be remapped to L/R Joystick, WASD, mouse controls, or custom inputs. The mapping interface features a categorized pop-up window to streamline the selection process.
* **Joy-Con IR Sensor Mapping:** Each Left and Right Joy-Con IR Sensor can be configured as an independent physical input. Per-side settings include Function mapping, Activation Threshold, and IR Mouse sensitivity.
* **Advanced Custom Input Remapping:** Featuring a powerful  "Custom" mapping feature. Users can record and assign any complex combination of keyboard keys, mouse clicks, or controller buttons to a single input. This flexible system supports both "Tap" (fires the recorded sequence momentarily) and "Hold" (sustains the sequence for as long as the button is pressed) modes.
  * Click the dropdown menu and select the "Custom" option.
  * Press and hold your desired combination of keyboard keys, mouse clicks, and/or controller buttons simultaneously.
  * Release all inputs. The recording will automatically stop and save your sequence.
  * Click the adjacent toggle button to switch between Tap (triggers the sequence once) and Hold (keeps the sequence pressed as long as you hold the controller button).
  * Click the X button to remove custom input and fall back to the default.
* **Mode Shift Mapping System:** Applies an alternative button mapping layer utilizing the In-app Gyro mapping store. The Mode Shift layer is activated via a dedicated mapping option supporting both Hold (active while held) and Tap (toggle) logic. Tap and Hold share a unified state machine, where a Hold action temporarily inverts a Tap-entered Mode Shift. Additionally, entering In-app Gyro mode can automatically apply the Mode Shift layer based on per-profile Gyro Control settings.
* **Emulation-Specific Mapping Categories:** Stores button mappings and rumble settings separately for Xbox, PS4, PS5, and Switch 2 emulation modes. The corresponding configuration is loaded when the emulation mode changes.
* **Custom Mapping Profile System:** A comprehensive profile management system allows users to create, rename, delete, and switch between multiple configurations. Each profile persistently stores button mappings, emulation mode, and driver settings. Profiles can be managed via a dedicated pop-up window that also configures the "Change Profile List" and "Profile Switching Combo" inputs. Features three seamless profile switching methods:
  * Profile Switching Combo: Users can record a custom Profile Switching Combo Trigger and assign specific Combo inputs for dedicated profiles. Pressing the Trigger input and a profile's Combo input simultaneously instantly switches to that dedicated profile. Both inputs function as standard mappings when not combined.
  * Auto Change Profile: Automatically switches to the selected checked profile in the Change Profile List after 2 seconds of trigger inactivity.
  * Manual Change Profile: Opens a selection notification. Navigate through the checked profiles using the L/R joysticks or Dpad Up/Down, and use the current Xbox/Switch layout A to confirm or B to cancel.
    * Auto/Manual Mode Setting: Toggle between Auto and Manual modes via the pop-up window by clicking the "Change Profile" button.
* **Assign Profile To Application:** Featuring profile auto-switching based on active foreground application. Bind one or more executable files (.exe) to a profile; when any of those applications become the focused window, the profile automatically activates.
* **Dynamic Split & Merge System:** The  **Split** and **Merge** features allow you to detach combined Joy-cons into two individual controllers or combine single Joy-cons into one unified virtual gamepad without restarting.
* **Vertical & Horizontal Hold Modes Switch (V/H):** Featuring V/H switch buttons, allowing users to toggle between Vertical (standard upright) and Horizontal (sideways) hold modes for single Joy-cons.
* **Per-Joy-Con V/H Mode Persistence:** The application records and remembers whether each single Joy-Con is held vertically or horizontally. Layout preferences (Vertical or Horizontal) are dynamically mapped to each controller's Bluetooth MAC address and saved in `config.yaml`.
* **Dual-Controller Gyro Selection (L/R Gyro):** When using a pair of Joy-cons as a single virtual controller, you can manually select which Joy-con (Left or Right) provides the motion data. This allows for greater flexibility, letting you choose your preferred hand for gyro aiming or motion controls.
* **Customizable Rumble Strength:** Adjusts vibration intensity from 0 to 10.
* **Rumble Frequency Slider:** Selects the vibration frequency used for translated rumble output.
* **Rumble Delay Configuration:** Adds a configurable delay in milliseconds for synchronizing vibration with game audio.
* **Dual Rumble Mode Toggle:** Switches between Xbox-style translated rumble and Switch HD Rumble output.
  * Xbox Mode: Tailored for standard PC games to simulate dual-motor rumble by activating dynamic frequency scaling and high-frequency masking to mimic traditional gamepad motors.
    * Strength 5 and Frequency 10 emulates the feel of a DualSense Edge controller.
    * Strength 10 and Frequency 10 emulates the rumble of an Xbox Elite Series 1 Controller.
  * Switch Mode: Mimics the native Switch HD Rumble (LRA) experience. It bypasses custom frequency scaling and masking, routing raw frequency data directly to the controller for a tighter, softer, and more detailed tactile feedback. Best suited for native Nintendo game emulations.
* **Interactive Controller Identification:** Featuring a dedicated **Vibrate** button for each player slot. This allows for instant physical feedback, helping you quickly identify which Joy-Con belongs to which player in a multiplayer setup.
* **Haptic & OS Integration:** Featuring rumble feedback (including a connection confirmation rumble) and mapping the Capture button to native Windows screenshots (`Win + PrtScn`).
* **One-Click Disconnect:** Featuring a convenient 'X' button to the top right of each connected controller's UI block. You can manually disconnect specific controllers directly from the interface without needing to power them off physically.
* **Auto-Disconnect Options:** Featuring a 3-way Auto-Disconnect toggle (OFF, Inactive, Absolute).
  * Inactive: tracks physical button and stick inputs to automatically disconnect idle controllers while keeping active players connected.
  * Absolute: tracks the overall time each controller is connected to the app and disconnects the ones that reach the time limit.
* **Driver Management Controls:** Provides Install, Repair, and Uninstall actions for the selected WinUHid, ViGEmBus, or USBIP backend. Driver status is determined from the active device, Driver Store package, service registration, and runtime availability instead of saved configuration alone.
* **ESP32-S3 Bridge Firmware Management:** Provides firmware installation and repair, BOOT-mode detection, reconnection handling, and flash diagnostics. When flashing fails, the application records esptool output in a diagnostic log.
* **Run at Startup:** Automatically launches the application with Windows.
* **Start Minimized:** Starts the application in the system tray.
* **Hide to System Tray:** Minimizes the application to the Windows system tray.
* **Controller UI Navigation:** Use the left joystick or D-pad to navigate the application interface. The selected UI element is indicated by a white outline, and the outline hides after mouse interaction or when Navigation mode is exited with the B button.
  * **Active Window Focus:** When a floating window or pop-up dialog is open, navigation remains within that active top-level interface.
  * **Return Selection:** Pressing B closes the active floating window and returns selection to the control that opened it. Re-entering Navigation mode restores the last selected control.
  * **UI Component Interaction:** Press A to activate buttons or open dropdown menus. Press B to close an open floating window or exit Navigation mode.
  * **Text Input Adjustment:** Text input fields support value adjustment with the right joystick or by holding A while using the left joystick. Continuous adjustment accelerates while the input is held.
  * **Slider and Time Input Adjustment:** Sliders can be adjusted with the right joystick or A plus the left joystick.
* **Window Position Persistence:** Saves and restores the main window position.
* **Standalone Executable (.exe):** Includes the required Python runtime and application dependencies; a separate Python installation is not required.

## Known Limitations

* **No Amiibo Support:** Amiibo support is not implemented.
* **No Switch 2 Pro Controller Audio Support:** Wireless audio transmission for the Switch 2 Pro Controller headphone jack and microphone is not supported.
* **No Working NSO GameCube Controller Gyro:** Gyro data from the NSO GameCube Controller cannot be decoded correctly.
* **No NSO GameCube Controller Rumble Brake & Strength Difference:** The Rumble Motor Brake command is not known, so PWM-based rumble strength control cannot be implemented correctly. Only basic rumble motor on/off control is currently implemented.
* **No Switch 2 Joy-Con Charging Grip Back Buttons Support:** Back buttons on the Switch 2 Joy-Con Charging Grip are not supported.

## System Requirements

* **Operating System:** Windows 10 (22H2 or above) or Windows 11.
    * *Note:* **Windows 11 is highly recommended** for the best experience. It supports a maximum Bluetooth LE polling rate of **70Hz**, while Windows 10 is limited to **20Hz** due to the lack of OS driver support for the BLE protocol.
* **Bluetooth Hardware:** Bluetooth 5.0 or above is required for stable connectivity and low-latency performance. The optional ESP32-S3 N16R8 is highly recommended for reaching 133Hz, bringing the native Switch 2 console experience to PC.
* **Driver:** [lurebat's WinUHid driver](https://github.com/lurebat/WinUHid) is required for Xbox One, PS4, and PS5/DualSense controller emulation. [nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus) is required for Xbox360 and PS4 controller emulation. [usbip-win2 driver](https://github.com/vadimgrn/usbip-win2) is required for Switch 1 Joy-Cons/Pro Controller, Switch 2 Pro Controller, and PS5/DualSense (with audio haptics) controller emulation.
    * *Auto-Installation:* The app will automatically detect if the selected driver (WinUHid, ViGEmBus, or USBIP) is missing and guide you through a one-click installation (requires administrator privileges) or automatically download and install for ViGEmBus.

### ESP32-S3 N16R8

<img width="447" height="235" alt="ESP32-S3 N16R8 Guide" src="https://github.com/user-attachments/assets/6bb295fb-bc2c-4cb4-b859-ead65429ee64" />

* **ESP32-S3 N16R8 Firmware Installation Guide:**

1.  Hold the boot button on the ESP32-S3 N16R8 board.
2.  Plug in the ESP32-S3 N16R8 board's OTG  port via USB-C to the PC while holding the boot button.
3.  In the app, click the **[ESP32-S3 N16R8 Driver]** button.
4.  Click click **[Install]** and wait until finish installing.
5.  Unplug and plug the ESP32-S3 N16R8 board.
6.  Reconnect any controllers previously paired via the system BLE by pressing SYNC.

* **ESP32-S3 N16R8 Buying Guide:**
1.  Search for development boards strictly labeled as **ESP32-S3 N16R8**. This ensures the board contains 16MB Flash and 8MB PSRAM, which is necessary for handling complex tasks and controller connections. Avoid any boards labeled as "N8R2", "N8R8", or standard "ESP32".
2.  Select the **ESP32-S3-WROOM-1** version if you want a built-in PCB antenna. This is the recommended, plug-and-play choice for standard plastic enclosures. Only choose the ESP32-S3-WROOM-1U version if you are using a metal case or require an external antenna to extend the Bluetooth/Wi-Fi range.
3.  Verify that the board features an **OTG USB port** (often a dual USB-C design). The OTG port is strictly required for data transfer and firmware installation.

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

<p align="left">
  <a href="https://ko-fi.com/tagayama">
    <img width="200" src="https://storage.ko-fi.com/cdn/brandasset/v2/support_me_on_kofi_beige.png?_gl=1*1ndc5yc*_gcl_au*MTAzOTY3NTA1Ni4xNzgyODE2Nzkx*_ga*MTQ5NTE2MDk5MC4xNzgyODE2Nzky*_ga_M13FZ7VQ2C*czE3ODQzMDg3NDckbzQ0JGcxJHQxNzg0MzA5NTQxJGo1OSRsMCRoMA.." alt="ko-fi">
  </a>
</p>

Happy gaming, and thank you for your generous support!

## Credit
**This project is developed based on and has been extensively modified from [Nadeflore/switch2-controllers](https://github.com/Nadeflore/switch2-controllers). I would like to thank the original author for her foundational work.**
* **[Nadeflore/switch2-controllers](https://github.com/Nadeflore/switch2-controllers):** The core foundation of this project.
* **[TheFrano/joycon2py](https://github.com/TheFrano/joycon2py), [german77/JoyconDriver](https://github.com/german77/JoyconDriver), [darthcloud/BlueRetro](https://github.com/darthcloud/BlueRetro/issues/1249):** The script and reverse-engineering information that the original switch2-controllers project was based on.
* **[nefarius/ViGEmBus](https://github.com/nefarius/ViGEmBus), [lurebat/WinUHid](https://github.com/lurebat/WinUHid), [vadimgrn/usbip-win2](https://github.com/vadimgrn/usbip-win2):** The drivers for virtual controller emulations.
* **[ndeadly/switch2_controller_research](https://github.com/ndeadly/switch2_controller_research):** Reverse-engineering for virtual Switch 2 Pro Controller emulation and real wired Switch 2 Pro Controller input translation.
* **[dekuNukem/Nintendo_Switch_Reverse_Engineering](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering):** Reverse-engineering for Switch 1 Pro Controller and Joy-Cons emulation.
* **[mart1nro/joycontrol](https://github.com/mart1nro/joycontrol):** Reference for Switch 1 Pro Controller and Joy-Cons emulation.
* **[LeonChrome/XinHeLianSheng-Pro2-Bridge](https://github.com/LeonChrome/XinHeLianSheng-Pro2-Bridge):** Inspiration for the ESP32-S3 N16R8 implementation. Also a reference for DualSense audio haptics and the USBIP-based Switch 2 controller emulation implementation.
* **[SundayMoments/DS5_Bridge](https://github.com/SundayMoments/DS5_Bridge):** Main reference for DualSense audio endpoint HID descriptor.
* **[JibbSmart/JoyShockLibrary](https://github.com/JibbSmart/JoyShockLibrary):** Reference for Switch 1 Joy-Cons gyro direction.
* **[RyanCopley/NSO-GameCube-Controller-Pairing-App](https://github.com/RyanCopley/NSO-GameCube-Controller-Pairing-App):** Reference for all NSO GameCube Controller related features. Also a reference for BLE throughput optimized mode.
