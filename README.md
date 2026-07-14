# <img width="30" height="30" alt="icon" src="https://github.com/user-attachments/assets/a2b3be7e-851d-4c55-8803-3de00a85eaeb" /> Switch 2 Connect

The ultimate app to connect your Switch 2 Joy-Cons, Switch 2 Pro Controller, and NSO GameCube Controller with standard Bluetooth or ESP32-S3 N16R8 and seamlessly integrate them into the Windows gaming ecosystem. 

## Quick Start

1. Download the `.exe` from the **[Releases](https://github.com/TommyWabg/Switch2Connect/releases)** page.
2. Launch the app. If the WinUHid driver is not installed, a dialog will ask to install it. If you select the USBIP driver mode in the settings, a dialog will ask to install the USBIP driver. Click **Yes** and approve the administrator UAC prompt.
3. Once the installation completes, the setup window will close automatically, and the main application will launch.
4. Turn on your Switch 2 controller by holding the Sync button (or pressing any button if already paired). **Do not** pair controllers manually in Windows Bluetooth settings; the app uses automatic GATT discovery.
5. Use the settings panel at the bottom of the app to configure your preferred driver (WinUHid / ViGEmBus / USBIP) and controller layout, gyro sensitivity, and custom button mappings.

## Feature Descriptions

* **Windows 10 Native Compatibility:** Resolved the `AttributeError: property is not available...` crash. Runs flawlessly on Windows 10 (22H2 and above). Windows 11 is still recommended for a 70Hz max Bluetooth polling rate, while only 20Hz max on Windows 10 due to the lack of OS driver support for the BLE protocol.
* **Low Latency Bluetooth Mode:** The application now forces Windows Bluetooth LE into `ThroughputOptimized` mode upon connection. This drastically drops the connection interval, massively reducing native Bluetooth input delay across the board.
* **ESP32-S3 N16R8 Low-Latency Connection:** Added native support for the ESP32-S3 N16R8 development board as a low-latency BLE bridge, which enables 133Hz polling rate. The application provides firmware installation, controller pairing via the SYNC button, and seamless reconnection for controllers previously bonded to the bridge.
* **Wired Pro Controller Support:** Supports wired Switch 2 Pro Controller discovery. The **Wired Pro Controller** menu allows users to manage HidHide, toggle automatic discovery, and run a one-time manual scan when needed.
* **Flexible Multi-Driver & Emulation Backend (WinUHid, ViGEmBus & USBIP):** Upgraded the emulation backend to support a flexible multi-driver architecture. Users can now seamlessly toggle between the built-in WinUHid driver, the industry-standard ViGEmBus virtual gamepad emulator, and a USBIP-based emulation mode, which enables emulating a physical USB-connected Switch 2 Pro Controller.
A special thank you to LeonChrome for proactively reaching out and sharing their open-source project y700-switch2-pro-bridge, which served as a crucial reference for successfully building this Switch 2 emulation mode.
* **Dynamic Emu Mode Toggle:** You can now instantly switch between Xbox One, PS4 (DualShock 4), and PS5 (DualSense) emulation modes directly from the settings panel. This allows you to choose the best protocol for your specific game or platform without restarting the app.
* **Tabs Organized Settings Interface:** The settings interface is organized into distinct "Controller Mapping", "Mode Shift Mapping", and "Gyro Settings" tabs for easy navigation.
* **DualSense Audio Haptic Feedback Support:** Introduced the new USBIP-based PS5 emu mode, which can establish a 4-channel audio playback device to receive DualSense audio haptic feedback from supported games. This architecture unifies PS5 Audio Haptics, Traditional PS5 Rumble, and Xbox Rumble. Translated DualSense Adaptive Trigger signals to generate independent HD Rumble pulses, which specifically trigger upon physical trigger press (ZL/ZR) or when a payload change occurs while the physical trigger is actively held.
* **Native Motion Support (PS4/PS5 Mode):** Switching to PS4 or PS5 mode enables native motion sensor reporting via the DS4 or DualSense protocol. This provides enhanced compatibility for Steam Input and games that support native DualShock 4 or DualSense gyro features.
* **Cemuhook UDP Server Support:** Implemented the Cemuhook UDP server (127.0.0.1:26760) to transmit direct motion control data to Switch 1 emulators. To use this feature, select "Cemuhook" on the "Mode" toggle switch within the "Gyro Passthrough" settings panel.
Cemuhook Gyro Sensitivity Adjustment: Added a Sensitivity slider (levels 1-5) to the "Gyro Passthrough" panel. The sensitivity specifically applies a linear multiplier only to the horizontal rotation (Yaw) axis sent via the Cemuhook UDP protocol.
  * **levels 1:** Virtual Switch 1 Joy-cons turn 360 degrees when the physical Switch 2 Joy-cons turn 360 degrees.
  * **levels 5:** Matches real Switch 1 Joycon sensitivity.
* **On-the-Fly Layout Switching:** Toggle between **Nintendo Layout** (matching physical labels) and **Xbox Layout** (standard PC positioning) directly from the UI.
* **NSO GameCube Controller Layout Mapping:** The NSO GameCube Controller uses consistent ABXY layout mapping across Xbox, PlayStation, and Switch emulation modes. Xbox Layout maps buttons by physical position, while Switch Layout maps buttons by input function.
* **1000Hz Interpolation:** 1000Hz interpolation loop for ultra-smooth, jitter-free gyro motion rendering with both Switch 2 Right Joy-con and Pro Controller. **Gyro Mouse** and **Joy-con Mouse**now output smoother and lag-free movement at 1000Hz. Gyro data handed off to other external applications (such as third-party emulators) is transmitted at a consistent, high-frequency 1000Hz rate. This transmission is purely non-interpolated; rather than generating synthetic intermediate frames, which could introduce latency, the app simply increases the packet delivery rate of real-time physical updates to ensure maximum accuracy and zero artificial delay.
* **In-app Gyro Control (Mouse, R Joystick & Steering):** Utilize in-app gyro control for mouse aiming, or select R Joystick and Steering options. The Steering mode reads the controller's absolute tilt (accelerometer) and maps it directly to the Left Analog Stick's X-axis. Unlike the Mouse control mode, the R Joystick and Steering modes utilize a separate In-app Gyro mapping scope where all buttons default to their standard controller inputs to prevent unexpected mouse clicks during controller emulation.
* **9-Axis Mouse Mode (Magnetometer Support):** A 9-axis motion controled mouse by leveraging the controllers' magnetometer. This provides absolute orientation tracking and eliminates long-term yaw drift. 
* **6-Axis Mouse Mode:** Play shooters or navigate through UI with high-polling rate gyro mouse control. RT and LT act as left and right mouse clicks when the gyro mouse is activated. This mode self-levels horizontal and vertical input regardless of controller tilt.
* **Gyro Racing Wheel Mode (Steering):** Reads the controller's absolute tilt (accelerometer) and maps it directly to the Left Analog Stick's X-axis.
* **Stick Assist:** Allowing the right thumbstick to work alongside gyro aiming.
* **In-App Gyro Trigger Deadzone:** Configurable within the In-App Gyro mapping pop-up window. Assign one or more buttons to apply a dedicated Trigger Deadzone while In-App Gyro is active. Users can customize the deadzone amount, gyro pause duration after button press and release, and how long the deadzone effect remains active after release. This helps reduce unintended gyro movement caused by button presses, releases, or controller vibration.
* **In-App Gyro Trigger Dampening:** Configurable within the In-App Gyro mapping pop-up window. Assign one or more buttons to proportionally reduce gyro sensitivity by a customizable percentage. Users can also customize how long the dampening effect remains active after the assigned button is released, allowing smoother control during aiming, steering, or other gyro-based actions.
* **In-app Gyro Lock:** A dedicated mapping option to pause gyro control while remaining in In-app Gyro mode, supporting both Hold and Tap activation logic.
* **Gyro Pass-through:**
  * **9-Axis Assist:** Integrated the 9-axis IMU fusion bias correction directly into the raw sensor reading pipeline. Using the magnetometer to continuously correct yaw drift for pass-through gyro data.
  * **Horizon Lock:** Added a toggle switch to apply horizon lock to passthrough gyro data. When enabled, it applies roll compensation and maintains the horizontal level. It disables roll data passthrough in this mode, eliminating off-axis cursor drift, roll crosstalk, and gimbal lock.
  * **Adjustable Soft Deadzone Sliders:** Dedicated sliders for adjusting soft deadzone values for both In-App Gyro and Passthrough gyro data. Soft deadzone subtracts the active deadzone value from the input magnitude, ensuring output begins smoothly from 0.0 right at the threshold boundary and eliminating step-jump discontinuities.
* **Gyro Calibration:** **Calibrate Gyro** button to calculate and permanently save sensor bias, eliminating gyro drift.
* **Magnetometer Calibration:** **Calibrate Mag** button for 9-axis accuracy. Perform a "figure-8" motion to calibrate the magnetometer (with a [quick link](https://youtu.be/J_cZnPcW-Yw?si=QWSizI49NQ_5OkA7) to a video tutorial).
* **Dual Joy-con Gyro (DJG):** Introduced a gyro fusion system that combines motion data from both Left and Right Joy-cons when used as a merged pair for stutter-free aiming when ratcheting. This system designates a "Dominant" side for spatial orientation and uses the "Sub" side as an accelerator for larger movements. A magnitude threshold of 30 is applied to the sub side. It contributes to acceleration only when the dominant side exceeds this threshold. Sub side acceleration is strictly capped to a maximum of 2x the dominant movement, and its opposite directional movement will be ignored. When the dominant side's gyro is turned off, the sub side takes over control.
  * Navigate to the Dual Joy-Con Gyro (DJG) panel.
  * Click the DJG toggle to ON to enable the fusion engine.
  * Set the Dominant Side to Left or Right. The dominant side acts as the primary reference for direction and gravity, while the sub side provides acceleration.
* **DJG Trigger Mapping:** Added a dedicated "DJG" option to the Extra Button Mapping settings.
  * Assign the "DJG" action to any available extra button to serve as the hardware trigger for DJG features.
  * Pressing this mapped button during gameplay will execute the action defined by the current DJG Control Mode and DJG Activation settings.
* **DJG Control Modes:** Three modes to dictate how the mapped DJG trigger button behaves during gameplay.
  * Single Side Toggle: Toggle the gyro tracking of a single Joy-Con on or off independently.
  * Switch Dominant Side: Swap the Dominant and Sub roles between the Left and Right Joy-cons. Both sides are forced to be active upon switching.
  * Switch Gyro Side: Turn off the current gyro and activate the opposite Joy-Con's gyro exclusively. The Dominant Side setting syncs automatically.
  * Direct Merge: Combine motion input from both Joy-Cons directly without Dominant/Sub role switching.
* **DJG Activation Types: Trigger behavior options to support different input styles.
  * Toggle: Switch the DJG state once per button press.
  * Hold: Switch the DJG state when the button is pressed, and revert to the original state when the button is released.
* **Full Controller Remappability:** All buttons (including extra buttons like GL, GR, SL_L, SL_R, SR_L, SR_R, NSO GCN L/R Analog Trigger Click, Home, Capture, and Chat) can be fully remapped to Switch inputs, PlayStation inputs, In-app controls, Windows controls, mouse clicks, or recorded custom input. Joysticks can also be remapped to L/R Joystick, WASD, mouse controls, or custom inputs. The mapping interface features a categorized pop-up window to streamline the selection process.
* **Advanced Custom Input Remapping:** Introduced a powerful new "Custom" mapping feature. Users can now record and assign any complex combination of keyboard keys, mouse clicks, or controller buttons to a single input. This flexible system supports both "Tap" (fires the recorded sequence momentarily) and "Hold" (sustains the sequence for as long as the button is pressed) modes.
  * Click the dropdown menu and select the "Custom" option.
  * Press and hold your desired combination of keyboard keys, mouse clicks, and/or controller buttons simultaneously.
  * Release all inputs. The recording will automatically stop and save your sequence.
  * Click the adjacent toggle button to switch between Tap (triggers the sequence once) and Hold (keeps the sequence pressed as long as you hold the controller button).
  * Click the X button to remove custom input and fall back to the default.
* **Mode Shift Mapping System:** Applies an alternative button mapping layer utilizing the In-app Gyro mapping store. The Mode Shift layer is activated via a dedicated mapping option supporting both Hold (active while held) and Tap (toggle) logic. Tap and Hold share a unified state machine, where a Hold action temporarily inverts a Tap-entered Mode Shift. Additionally, entering In-app Gyro mode can automatically apply the Mode Shift layer based on per-profile Gyro Control settings.
* **Categorized Button Mapping Profiles:** Segregated custom button mapping and rumble configurations into three independent target categories: Xbox, PS4, PS5, and Switch 2. Remap profiles are now saved and loaded automatically based on the active emulation mode.
Custom Mapping Profile System: A comprehensive profile management system allows users to create, rename, delete, and switch between multiple configurations. Each profile persistently stores button mappings, emulation mode, and driver settings. Profiles can be managed via a dedicated pop-up window that also configures the "Change Profile List" and "Profile Switching Combo" inputs. Features three seamless profile switching methods:
  * Profile Switching Combo: Users can record a custom Profile Switching Combo Trigger and assign specific Combo inputs for dedicated profiles. Pressing the Trigger input and a profile's Combo input simultaneously instantly switches to that dedicated profile. Both inputs function as standard mappings when not combined.
  * Auto Change Profile: Maintains the Change Profile button behavior in previous versions. Automatically switches to the selected checked profile in the Change Profile List after 2 seconds of trigger inactivity.
  * Manual Change Profile: Opens a selection notification. Navigate through the checked profiles using the L/R joysticks or Dpad Up/Down, and use the current Xbox/Switch layout A to confirm or B to cancel.
    * Auto/Manual Mode Setting: Toggle between Auto and Manual modes via the pop-up window by clicking the "Change Profile" button.
* **Assign Profile To Application:** Added profile auto-switching based on active foreground application. Bind one or more executable files (.exe) to a profile; when any of those applications become the focused window, the profile automatically activates.
* **Joy-con Mouse Toggle:** A new dedicated switch in the GUI to enable or disable the Joy-con mouse mode. This prevents accidental cursor movement during gameplay.
* **Dynamic Split & Merge System:** The new **Split** and **Merge** features allow you to detach combined Joy-cons into two individual controllers or combine single Joy-cons into one unified virtual gamepad without restarting.
* **Vertical & Horizontal Hold Modes Switch (V/H):** Added V/H switch buttons, allowing users to toggle between Vertical (standard upright) and Horizontal (sideways) hold modes for single Joy-cons.
* **Per-Joy-Con V/H Mode Persistence:** The application now records and remembers whether each single Joy-Con is held vertically or horizontally. Layout preferences (Vertical or Horizontal) are dynamically mapped to each controller's Bluetooth MAC address and saved in `config.yaml`.
* **Dual-Controller Gyro Selection (L/R Gyro):** When using a pair of Joy-cons as a single virtual controller, you can now manually select which Joy-con (Left or Right) provides the motion data. This allows for greater flexibility, letting you choose your preferred hand for gyro aiming or motion controls.
* **Customizable Rumble Strength:** Introduced a new Vibration Strength slider in the settings panel (ranging from 0 to 10), allowing users to dynamically scale the intensity of the controller's haptic feedback.
Rumble Frequency Slider: You can now customize the vibration/rumble frequency directly from the settings panel.
Rumble Delay Configuration: Implemented a customizable rumble delay setting in milliseconds. This allows users to manually synchronize haptic feedback with audio for games where sound and vibration are misaligned.
* **Dual Rumble Mode Toggle:** Introduced a toggle switch in the user interface to easily switch between Xbox and Switch rumble modes.
  * Xbox Mode: Tailored for standard PC games to simulate dual-motor rumble by activating dynamic frequency scaling and high-frequency masking to mimic traditional gamepad motors.
    * Strength 5 and Frequency 10 emulates the feel of a DualSense Edge controller.
    * Strength 10 and Frequency 10 emulates the rumble of an Xbox Elite Series 1 Controller.
  * Switch Mode: Mimics the native Switch HD Rumble (LRA) experience. It bypasses custom frequency scaling and masking, routing raw frequency data directly to the controller for a tighter, softer, and more detailed tactile feedback. Best suited for native Nintendo game emulations.
* **Interactive Controller Identification:** Added a dedicated **Vibrate** button for each player slot. This allows for instant physical feedback, helping you quickly identify which Joy-Con belongs to which player in a multiplayer setup.
* **Haptic & OS Integration:** Added rumble feedback (including a connection confirmation rumble) and mapped the Capture button to native Windows screenshots (`Win + PrtScn`).
* **One-Click Disconnect:** Added a convenient 'X' button to the top right of each connected controller's UI block. You can now manually disconnect specific controllers directly from the interface without needing to power them off physically.
* **Auto-Disconnect Options:** Added a 3-way Auto-Disconnect toggle (OFF, Inactive, Absolute).
  * Inactive: tracks physical button and stick inputs to automatically disconnect idle controllers while keeping active players connected.
  * Absolute: tracks the overall time each controller is connected to the app and disconnects the ones that reach the time limit.
* **Dedicated UI Driver Controls:** Added an **Install/Uninstall WinUHid Driver** button to the left of the "Run At Startup" button.
* **Run at Startup:** Added a toggle to automatically launch the application with Windows.
* **Start Minimized:** Option to launch directly to the system tray for a seamless background experience.
* **Hide to system tray:** Added the ability to minimize the application to the Windows system tray.
* **Controller UI Navigation:** Implemented UI navigation using the left joystick or D-pad. The currently selected UI element is indicated by a white outline. The outline automatically hides upon detecting mouse clicks anywhere within the application or pressing the B button on the controller.
  * UI Component Interaction: Added controller support for interacting with UI elements. Pressing the 'A' button clicks buttons or opens dropdown menus, while the 'B' button closes open dropdown menus without applying changes.
  * Slider and Time Input Adjustment: Enabled adjustment of sliders and auto-disconnect time input fields using the right joystick. Alternatively, holding the 'A' button while using the left joystick modifies the values without triggering spatial navigation.
* **Window Position Persistence:** The application now saves and restores the window's position on the screen.
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
