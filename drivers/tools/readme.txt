


SoundVolumeView v2.51
Copyright (c) 2013 - 2026 Nir Sofer
Web site: https://www.nirsoft.net



Description
===========

SoundVolumeView is a simple tool for Windows Vista/7/8/2008/10/11 that
displays general information and current volume level for all active
sound components on your system, and allows you to mute and unmute them
instantly.
SoundVolumeView also allows you to save a sound profile into a file,
containing the current volume level and the mute/unmute state of all
sound components, as well as the default sound devices, and then later,
load the same file to restore exactly the same volume levels and settings.
There is also extensive command-line support, which allows you to
save/load profiles, change current volume of every sound component, and
mute/unmute every sound component, without displaying any user interface.



System Requirements
===================

This utility works on Windows Vista, Windows 7, Windows 8, Windows 2008,
Windows 10, and Windows 11. Both 32-bit and 64-bit systems are supported.
Windows XP and older systems are not supported.



Versions History
================


* Version 2.51:
  o Added 'Copy Clicked Cell' option to the right-click context menu,
    which copies to the clipboard the text of cell that you right-clicked
    with the mouse.

* Version 2.50:
  o Added 'Cursor' option to all command-line options, which allows
    you to change the volume of the application under the mouse cursor.
    For example, the following command mutes/unmutes the application
    under the mouse cursor:
    SoundVolumeView.exe /Switch Cursor
  o Added 'CursorName' option to all command-line options, which
    allows you to change the volume of all instances of the application
    under the mouse cursor.

* Version 2.49:
  o Fixed issue with setting default format of a device to 24-bit on
    some systems (/SetDefaultFormat command).

* Version 2.48:
  o Updated the /SetSpatial command to work on Windows 11 25H2.

* Version 2.47:
  o Added 'Default Format' column.

* Version 2.46:
  o Added /SetBooleanFxProperty command-line option, which allows you
    to turn on/off different options of a device, like 'Loudness
    Equalization', 'Headphone Virtualization', 'Voice Cancellation', and
    others.

* Version 2.45:
  o Added /GetColumnValue command-line option, which allows to get a
    value of specific column for the specified item, for example:
    SoundVolumeView.exe /GetColumnValue "Realtek High Definition
    Audio\Device\Speakers\Render" "Speakers Config" | more

* Version 2.43:
  o The 'Device State' column now also displays state information for
    application items (Active/Inactive/Expired).

* Version 2.42:
  o Added /SaveDeviceFormat and /LoadDeviceFormat command-line
    options, which allow you to save the data of the device default
    format into a file, and then later load it back into the device.
  o You should use these new commands if you have a format that
    cannot be set with the /SetDefaultFormat command.

* Version 2.41:
  o Added 'Black Background' option (Under the View menu). When it's
    turned on, the main table is displayed in black background and white
    text, instead of default system colors.

* Version 2.40:
  o Added 'Use Mouse Wheel On Tray Icon' option. When it's turned on,
    you can increase/decrease the sound volume of the selected item while
    the mouse cursor is over the tray icon. The percent of change for
    every wheel move is according to the 'Mouse Wheel % Change' option.

* Version 2.38:
  o Added 'Sort By' toolbar button.

* Version 2.37:
  o The current volume level of the selected item is now displayed in
    the tooltip of the tray icon. This feature works only when both 'Put
    Icon On Tray' option and 'Auto Refresh' option are turned on.

* Version 2.36:
  o Added /SetSpeakersConfig command-line option, which allows you to
    set the desired speakers configuration (Stereo, Quadraphonic, 5.1
    Surround, 7.1 Surround) from command-line.
  o Added 'Speakers Config' column, which displays 3 Hexadecimal
    values that represent the current speakers configuration. You can use
    these values in the /SetSpeakersConfig command-line option.

* Version 2.35:
  o Added 'Show Mute On Tray Icon' option. When it's turned on,
    SoundVolumeView displays mute icon on the tray if the selected item
    is muted.
  o This feature works only when both 'Put Icon On Tray' option and
    'Auto Refresh' option are turned on.

* Version 2.31:
  o Fixed a few high DPI mode issues.

* Version 2.30:
  o Added /Columns command-line option, which allows you to set the
    columns to display or the columns to export from command-line, for
    example:
    SoundVolumeView.exe /scomma "c:\temp\sound-items.csv" /Columns
    "Name,Command-Line Friendly ID,Volume Percent"

* Version 2.29:
  o You can now use wildcard with the command-line friendly ID, for
    example:
    SoundVolumeView.exe /Switch "Realtek*\Device\Speakers\Render"

* Version 2.28:
  o You can now specify the number of channels in the
    /SetDefaultFormat command, for example:
    SoundVolumeView.exe /SetDefaultFormat "Speakers" 24 48000 4

* Version 2.27:
  o Updated the /SetAppDefault and /SetSpatial commands to work on
    Windows 11.

* Version 2.26:
  o You can now use any variable inside the .cfg file
    (SoundVolumeView.cfg) in order to set the configuration from command
    line, for example:
    SoundVolumeView.exe /SaveFileEncoding 3 /ShowUnpluggedDevices 1
    /ShowDisabledDevices 1

* Version 2.25:
  o You can now choose the desired encoding (ANSI, UTF-8, UTF-16) to
    save the csv/xml/text/html files. (Under the Options menu)
  o Updated the HTML export feature to HTML5.
  o Added option to export as JSON file.
  o You can now specify 'System Sounds' as the item name from
    command-line. For example, this command mutes the system sounds from
    command-line:
    SoundVolumeView.exe /Mute "System Sounds"

* Version 2.23:
  o Added secondary sorting support: You can now get a secondary
    sorting, by holding down the shift key while clicking the column
    header. Be aware that you only have to hold down the shift key when
    clicking the second/third/fourth column. To sort the first column you
    should not hold down the Shift key.
  o Added option to change the sorting column from the menu (View ->
    Sort By). Like the column header click sorting, if you click again
    the same sorting menu item, it'll switch between ascending and
    descending order. Also, if you hold down the shift key while choosing
    the sort menu item, you'll get a secondary sorting.

* Version 2.22:
  o Updated to work properly in high DPI mode.

* Version 2.21:
  o Added 'all' option to the /SetDefault and /SwitchDefault
    command-line options, which allow you to set all 3 default types
    (Console, Multimedia, Communications) at once, for example:
    SoundVolumeView.exe /SetDefault "High Definition Audio
    Device\Device\Speakers\Render" all
  o Added 'Set Default Device - All' to the Create Shortcut menu.

* Version 2.20:
  o You can now use the /SetListenToThisDevice and
    /SetPlaybackThroughDevice command-line options without running
    SoundVolumeView as Administrator.
  o Added /SetAllowExclusive command-line option to set the 'Allow
    applications to take exclusive control of this device' option of a
    device.
  o Added /SetExclusivePriority command-line option to set the 'Give
    exclusive mode applications priority' option of a device.

* Version 2.16:
  o Added 'all' option to the /SetAppDefault command-line option,
    which allows you to set all 3 default types (Console, Multimedia,
    Communications) at once, for example:
    SoundVolumeView.exe /SetAppDefault "High Definition Audio
    Device\Device\Speakers\Render" all "chrome.exe"

* Version 2.15:
  o Added /SetSpatial command-line option, which allows you to set
    the 'Spatial sound format' of sound device from command line on
    Windows 10, for example:
    SoundVolumeView.exe /SetSpatial "High Definition Audio
    Device\Device\Speakers\Render" "Windows Sonic For Headphones"

* Version 2.11:
  o Added support for 'focused' and 'focusedname' in the
    /SetAppDefault command for setting the default input or output device
    of the focused application on Windows 10, for example:
    SoundVolumeView.exe /SetAppDefault "High Definition Audio
    Device\Device\Speakers\Render" 0 focused
  o Added /SetDefaultFormat command line option, which allows you to
    set the default format of the device. For example: The following
    command sets the default format of the device to '24 bit, 192000
    Hz(Studio Quality)':
    SoundVolumeView.exe /SetDefaultFormat "Realtek High Definition
    Audio\Device\Speakers\Render" 24 192000

* Version 2.10:
  o Added /SetAppDefault command-line option, which allows you to set
    the default render/capture device for specific application on Windows
    10, for example:
    SoundVolumeView.exe /SetAppDefault "High Definition Audio
    Device\Device\Speakers\Render" 0 "chrome.exe"
  o Fixed bug: SoundVolumeView displayed the wrong device on
    application volume items.
  o Added 'Show Unplugged Devices' option.

* Version 2.06:
  o Added 'Select All' and 'Deselect All' to the 'Column Settings'
    window.

* Version 2.05:
  o Added 'Registry Key' column, which displays the Registry key of
    audio device.
  o Added 'Open Device Key In RegEdit' option, which allows you to
    open the device Registry key in RegEdit.

* Version 2.00:
  o Added option to easily create shortcuts on desktop that will
    execute one of the following actions: Mute,Unmute,Mute/Unmute
    Switch,Disable,Enable,Disable/Enable Switch, Set Default Device
    (Console, Multimedia, Communications), Increase Volume, Decrease
    Volume.
  o In order to create the desktop shortcut, simply select the
    desired item, and then go to File -> Create Desktop Shortcut and
    choose the desired action to execute when the shortcut is activated.
    You can also use the 'Create Desktop Shortcut' submenu in the
    right-click context menu.
  o The following actions can also be used with application items:
    Mute,Unmute,Mute/Unmute Switch,Increase Volume, Decrease Volume. For
    example, you can create 'Mute/Unmute Switch' shortcut for Firefox
    that will mute the sound of Firefox if it's turned on and unmute if
    it's turned off.
  o After creating the shortcut, you can right-click on it, choose
    'Properties' and then choose the desired key combination to activate
    the shortcut.

* Version 1.90:
  o Added 'Default Multimedia' and 'Default Communications' columns,
    which display the Multimedia and Communications default types. (The
    'Default' column displays the 'Console' default).
  o Added option to specify default Multimedia/Communications device
    instead of name: DefaultRenderDeviceMulti, DefaultCaptureDeviceMulti,
    DefaultRenderDeviceComm, DefaultCaptureDeviceComm.

* Version 1.87:
  o The 'Command-Line Friendly ID' now specifies whether the device
    is a capture or render device.

* Version 1.86:
  o Added new command-line options: /GetDecibel , /GetDecibelChannel

* Version 1.85:
  o Added new command-line options: /SetListenToThisDevice ,
    /SetPlaybackThroughDevice , /RunAsAdmin

* Version 1.83:
  o Fixed bug: SoundVolumeView failed to remember the last
    size/position of the properties window if it was not located in the
    primary monitor.
  o You can now send the data to stdout by specifying empty string as
    filename, for example:
    SoundVolumeView.exe /scomma "" | more

* Version 1.82:
  o You can now resize the properties window, and the last
    size/position of this window is saved in the .cfg file.

* Version 1.81:
  o Added /ChangeVolumeChannel command-line option, for changing the
    volume of specific channel.

* Version 1.80:
  o Added 'Selected Channel' option (Under the Options menu). If you
    want to change the volume of specific channel, you can choose the
    desired channel from this menu, and then use the Increase/Decrease
    1%/5%/10% options (under the Volume menu) or the mouse wheel to
    change the volume of the selected channel.

* Version 1.75:
  o Added support for setting the volume of specific channels for
    specific application from command-line. For example, the following
    command sets the volume of the left channel to 50% only for Chrome
    Web browser:
    SoundVolumeView.exe /SetVolumeChannels "chrome.exe" 50 100
  o Also, the current channels volume of application is displayed in
    the 'Channels Percent' column.
  o Added 'focusedname' to all command-line options, which allows you
    to change the volume all instances of the focused application.
  o Fixed bug: When using 'focused' in command-line and but there was
    no focused application, SoundVolumeView changed the 'System Sounds'
    items.

* Version 1.72:
  o Added 'Align Numeric Columns To Right' option.

* Version 1.71:
  o You can now specify "DefaultCaptureDevice" and
    "DefaultRenderDevice" in the [Name] parameter of all command-line
    options in order to refer the default render/capture device.

* Version 1.70:
  o Added /WaitForItem command-line option, which instructs
    SoundVolumeView to wait until the sound item appears. You can use
    this feature to change the application volume for applications that
    are currently not running. For example, if you want to mute the sound
    of Chrome Web browser and it's not running at this moment:
    SoundVolumeView.exe /Mute chrome.exe /WaitForItem 0
  o Disabled devices are now displayed with disabled icon.

* Version 1.66:
  o Added /GetPercentChannel command-line option.

* Version 1.65:
  o Added 'Command-Line Friendly ID' column. You can use the string
    displayed in this column in all command-line options when you have
    multiple items with the same name (For example: 2 'Speakers'
    devices). This ID is more friendly than the ID provided by Windows
    operating system ('Item ID' column), for example: "Realtek High
    Definition Audio\Device\Speakers" and "2- USB
    AUDIO\Device\Microphone".

* Version 1.60:
  o Added 'Disable Device' and 'Enable Device' options.
  o Added 'Show Disabled Devices' option, when it's turned on,
    SoundVolumeView displays disabled items.
  o Added 'Device State' column - Displays whether a device is active
    or disabled.
  o Added new command-line options - /Disable , /Enable ,
    /DisableEnable

* Version 1.57:
  o Added 'Copy Mute/Unmute Command' option (Ctrl+M), which copies to
    the clipboard a command to mute and unmute the selected sound
    component (Using the /Switch command-line option).

* Version 1.56:
  o Added /ChangeVolumeDecibel command-line option, which allows you
    to increase/decrease the volume of devices and subunits in Decibel,
    for example:
    SoundVolumeView.exe /ChangeVolumeDecibel "Microphone Boost" -10

* Version 1.55:
  o Added 'Decrease Volume Step (dB)' (Ctrl+7) and 'Increase Volume
    Step (dB)' (Ctrl+8) options which increase/decrease the volume
    accoding to the default volume step displayed in 'Volume Step' column.
  o Added 'Export All Items' option.

* Version 1.50:
  o Added 'Direction' column (Capture or Render)
  o Added /SetVolumeChannelsDecibel command-line option, which allows
    you to set the channel volume of devices and subunits in Decibel, for
    example:
    SoundVolumeView.exe /SetVolumeChannelsDecibel "Speakers" -20.5 -18.5
  o Added /SetVolumeDecibel command-line option, which allows you to
    set the volume of devices and subunits in Decibel, for example:
    SoundVolumeView.exe /SetVolumeDecibel "Speakers" -14
  o Added Subunits of capture devices (Like 'Microphone Boost')
  o You can set the 'Microphone Boost' value from command-line, for
    example:
    SoundVolumeView.exe /SetVolumeDecibel "Microphone Boost" 30
  o Fixed bug: When using '*' with devices on /SetVolumeChannels
    command-line option, SoundVolumeView set the volume channel to zero
    instead of leaving it without change.

* Version 1.45:
  o Added 'Save Sound Profile - Selected Items' option (Alt+S), which
    creates a profile only from the items you select.

* Version 1.43:
  o Added /GetMute command-line option that returns the current Mute
    status.

* Version 1.42:
  o Added option to return the current volume level in percent (for
    using in scripts and batch files). The return value is the percent
    value multiplied by 10, for example:
    SoundVolumeView.exe /GetPercent Speakers
    echo %errorlevel%

* Version 1.41:
  o Added option to set the percent of change for every wheel move in
    the 'Change Volume With Mouse Wheel' option ('Mouse Wheel % Change'
    menu)

* Version 1.40:
  o Added 'Change Volume With Mouse Wheel' option, which allows you
    to increase/decrease the volume of selected items by scrolling the
    mouse wheel. You can choose to change the volume with the mouse wheel
    when the Ctrl key is down or when the left mouse button is down or
    when the middle button is down. The default option is 'When Ctrl key
    is down'.

* Version 1.36:
  o You can now specify only a part of name or id of the sound item
    in all command-line options ( /SetVolume, /Mute , /Unmute , and so
    on...), for example: If ID of a subunit is
    "{2}.\\?\hdaudio#func_01&ven_10ec&dev_0889&subsys_1458a002&rev_1000#4&3
    828eb94&0&0201#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\singlelineouttopo
    /0002000c" , the following command will mute this subunit:
    SoundVolumeView.exe /Mute "singlelineouttopo/0002000c"

* Version 1.35:
  o Added 'Auto Size Columns On Update' option.

* Version 1.33:
  o You can now use the /SetVolumeChannels command-line option with
    devices.

* Version 1.32:
  o Added option to set from command-line the volume of focused
    application, for example:
    SoundVolumeView.exe /SetVolume focused 50

* Version 1.31:
  o Fixed bug: When there were multiple application volume items of
    the same process filename, and you tried to set one of them,
    SoundVolumeView set the wrong item.

* Version 1.30:
  o SoundVolumeView now displays the process names (Some of them
    without the full path) for most processes when you run it without
    elevation ('Run As Administrator').
  o Fixed the Ctrl+C (Copy Selected Items) key.
  o You can now specify the process ID in the /SetVolume
    /ChangeVolume /Mute /Unmute /Switch command-line options.

* Version 1.27:
  o Added 'Run As Administrator' option (Ctrl+F11), for viewing the
    icon and information of applications running as admin.

* Version 1.26:
  o Added /SwitchDefault command-line option, which allows you to
    switch between 2 default devices, for exmaple:
    SoundVolumeView.exe /SwitchDefault
    "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}"
    "{0.0.0.00000000}.{7747b192-73b2-47d3-a2c0-168e94af7f9e}" 0


* Version 1.25:
  o Added option to set from command-line the volume of all
    applications at once, for example:
    SoundVolumeView.exe /SetVolume AllAppVolume 50

* Version 1.22:
  o Fixed bug: SoundVolumeView failed to remember the last
    size/position of the main window if it was not located in the primary
    monitor.

* Version 1.21:
  o Added 'Always On Top' option.

* Version 1.20:
  o Added 'Process ID' and 'Window Title' columns (For application
    volume items).

* Version 1.15:
  o Added 'Copy Item ID' and 'Copy Item Name' options.

* Version 1.13:
  o Added accelerator keys to the 'Set Default' options.

* Version 1.12:
  o Fixed issue: /SaveProfile command-line option failed to save the
    sound settings file when running it from a batch file without
    specifying the full path of the sound settings file.

* Version 1.11:
  o Added option to choose another font (font name and size) to
    display the sound components list. (Options -> Select Another Font)

* Version 1.10:
  o Added 'Sort On Every Update' option. If it's turned on,
    SoundVolumeView will sort the list every time that a new item is
    added or an existing item is updated.

* Version 1.06:
  o Added 'Start As Hidden' option. When this option and 'Put Icon On
    Tray' option are turned on, the main window of SoundVolumeView will
    be invisible on start.
  o Added 'Clear Recent Files List' option.

* Version 1.05:
  o Added 'Set Default' options (Under the Volume menu) to set the
    default device.
  o Added documentation for /SetDefault command-line option (It was
    exist on v1.00, but I forgot to add it to the help file of
    SoundVolumeView...)

* Version 1.00 - First release.



Start Using SoundVolumeView
===========================

SoundVolumeView doesn't require any installation process or additional
dll files. In order to start using it, simply run the executable file -
SoundVolumeView.exe

After running SoundVolumeView, the main window is displays all sound
items found in your systems. There are 3 types of items: devices,
subunits, and application volume.
For every item, the current volume level is displayed in percent unit.
For devices and subunits, the volume level is also displayed in Decibel.
For subunits, the volume level is displayed for every channel separately.



Increase/Decrease/Mute Volume
=============================

In the main window of SoundVolumeView, you can select one or more items,
and then mute, unmute, increase, or decrease the volume of selected
items, using accelerator keys:
* Mute - F7
* Unmute - F8
* Mute/Unmute switch - F9
* Decrease volume 1% - Ctrl+1
* Increase volume 1% - Ctrl+2
* Decrease volume 5% - Ctrl+3
* Increase volume 5% - Ctrl+4
* Decrease volume 10% - Ctrl+5
* Increase volume 10% - Ctrl+6

The Increase/Decrease options can also be used for specific channel. In
order to do that, choose the desired channel from Options -> Selected
Channel.



Sound Profiles
==============

SoundVolumeView allows you to save all your current sound settings into a
sound profile filename, and then load it later when you want to restore
these settings.
The sound profile file stores the following information:
* Volume level of all active sound components on your system. (Devices,
  Subunits, and application volume) For Subunits, the volume level of
  every channel is stored separately.
* Mute/Unmute state of all active sound components on your system.
  (Devices, Subunits, and application volume)
* Default render/capture device.

You can save and load sound profiles by using the 'Save Sound Profile'
and 'Load Sound Profile' options under the File menu, or by using the
/SaveProfile and /LoadProfile command-line options. There is also a
recent menu located under the File menu, which allows you to easily load
the last 10 sound profiles you used, as well as you can also load the
recent 5 sound profiles from the tray menu (If the tray icon is turned on)

Be aware that sound profiles are bound to specific computer and its
devices. You cannot save a sound profile in one computer and then load it
into another computer.



Creating Desktop Shortcuts
==========================

Starting from version 2.00, you can easily create shortcuts on desktop
that will execute one of the following actions: Mute,Unmute,Mute/Unmute
Switch,Disable,Enable,Disable/Enable Switch, Set Default Device (Console,
Multimedia, Communications), Increase Volume, Decrease Volume.
In order to create the desktop shortcut, simply select the desired item,
and then go to File -> Create Desktop Shortcut and choose the desired
action to execute when the shortcut is activated. You can also use the
'Create Desktop Shortcut' submenu in the right-click context menu.
The following actions can also be used with application items:
Mute,Unmute,Mute/Unmute Switch,Increase Volume, Decrease Volume. For
example, you can create 'Mute/Unmute Switch' shortcut for Chrome Web
browser that will mute the sound of Chrome if it's turned on and unmute
if it's turned off.
After creating the shortcut, you can right-click on it, choose
'Properties' and then choose the desired key combination to activate the
shortcut.



Get sound level information from command line
=============================================

If you want to get the current sound volume or mute status from
command-line, there are 2 different methods you can use:
1. Use the get commands of SoundVolumeView - /GetPercent
   /GetPercentChannel, /GetDecibel, /GetDecibelChannel, /GetMute : These
   commands return the desired sound level information inside the Exit
   Status of the program. Because the Exit Status is an integer value,
   the percent value is multiplied by 10 and the Decibel value is
   multiplied by 1000. You can get more information about these commands
   in the 'Command-Line Options' section.
   You can get the desired value inside a batch file by using the
   %errorlevel% variable, for example.
   SoundVolumeView.exe /GetPercent Speakers
   echo %errorlevel%

   It's recommended to use the Get commands with the console version of
   this tool (SoundVolumeCommandLine), because this technique doesn't
   work well with GUI programs.
2. Use the combination of GetNir tool and SoundVolumeView to send the
   desired value to stdout.
   For example, the following command sends to stdout the current sound
   volume (in percent) of a device that its name is 'Speakers':
   SoundVolumeView.exe /stab "" | GetNir "Volume Percent" "Name=Speakers
   && Type=Device"

   The following command sends to stdout the current mute status (From
   the 'Muted' column) of the device that its friendly name is Realtek
   High Definition Audio\Device\Speakers\Render:
   SoundVolumeView.exe /stab "" | GetNir "Muted"
   "Command-LineFriendlyID='Realtek High Definition
   Audio\Device\Speakers\Render'"



Command-Line Options
====================

You can use the command-line options below to change the volume level and
mute/unmute status of every sound component on your system.
In the [Name] parameter , you can specify one of the following fields:
* The name of the item, as appeared under the 'Name' column. (If you
  have multiple items with identical name, you should use the ID field.)
* The ID of the item, as appeared under the 'Item ID' column.
* The ID of the item, as appeared under the 'Command-Line Friendly ID'
  column (This ID is generated by SoundVolumeView and it's designed to be
  more friendly than the 'Item ID' column). For Command-Line Friendly ID,
  you can also use wildcard, for example: USB*\Device\Speakers\Render
* You can also specify only a part of the 'Name' or 'Item ID'. For
  example, if the device name is 'Speakers1', specifying 'Speakers' will
  also work.
* For sound devices (Type = 'Device'), you can also use the value
  specified under the 'Device Name' column.
* For application items (type = 'Application), you can also specify the
  process filename, (for example: firefox.exe) or the process ID (for
  example: 3271).
* You can specify 'AllAppVolume' to change the volume of all
  applications at once. For example, in order to set the volume of all
  applications to 100%:
  SoundVolumeView.exe /SetVolume AllAppVolume 100
* You can specify 'Focused' to change the volume of focused application.
* You can specify 'FocusedName' to change the volume of all instances
  of focused application.
* You can specify 'Cursor' to change the volume of the application
  under the mouse cursor.
* You can specify 'CursorName' to change the volume of all instances of
  the application under the mouse cursor.
* You can specify "DefaultCaptureDevice" and "DefaultRenderDevice" in
  order to refer the default Console render/capture device. For default
  Communications device, you can specify DefaultCaptureDeviceComm and
  DefaultRenderDeviceComm, and for default Multimedia device you can
  specify DefaultRenderDeviceMulti and DefaultCaptureDeviceMulti.
* You can specify multiple commands in the same command-line, for
  example:
  SoundVolumeView.exe /Unmute "Speakers" /SetVolume AllAppVolume 100



/GetPercent [Name]
Returns the current volume level in percent, multiplied by 10, for using
in scripts and batch files.
It's recommended to use this command with the console version of this
tool (SoundVolumeCommandLine)

Example:
SoundVolumeView.exe /GetPercent Speakers
echo %errorlevel%


/GetMute [Name]
Returns the current Mute status. (1 = Muted, 0 = Not Muted)
It's recommended to use this command with the console version of this
tool (SoundVolumeCommandLine)

/GetPercentChannel [Name] [Channel Number]
Returns the current volume level of specific channel in percent,
multiplied by 10. In the [Channel Number] parameter you should specify 0
for the first channel, 1 for the second channel, and so on...
It's recommended to use this command with the console version of this
tool (SoundVolumeCommandLine)

/GetDecibel [Name]
Returns the current volume level in decibel, multiplied by 1000.
It's recommended to use this command with the console version of this
tool (SoundVolumeCommandLine)

Example:
SoundVolumeView.exe /GetDecibel Speakers
echo %errorlevel%


/GetDecibelChannel [Name] [Channel Number]
Returns the current volume level of specific channel in decibel,
multiplied by 1000. In the [Channel Number] parameter you should specify
0 for the first channel, 1 for the second channel, and so on...
It's recommended to use this command with the console version of this
tool (SoundVolumeCommandLine)

/GetColumnValue [Name] [Column Name]
Sends the value of the specified column to stdout. The [Column Name] is
the exact name of the column displayed in the GUI. Optionally, you can
omit the space characters from the column name (e.g: "DeviceState"
instead of "Device State")

Example:
The following command shows the state of the device that its Command-Line
Friendly ID is 'Realtek High Definition Audio\Device\Speakers\Render':
SoundVolumeView.exe /GetColumnValue "Realtek High Definition
Audio\Device\Speakers\Render" "Device State" | more

Be aware that the '| more' section is needed for viewing the output of
GUI application inside a command-prompt window. It's not needed if you
use the console version of this tool (SoundVolumeCommandLine).

/SetVolume [Name] [Volume]
Set the sound volume of the specified item. The [Volume] is a number
between 0 and 100.

/SetVolumeDecibel [Name] [Volume]
Set the sound volume of the specified item. The [Volume] is in decibels.

/ChangeVolumeDecibel [Name] [Volume]
Increase/decrease the sound volume of the specified item. The [Volume] is
in decibels.

/SetVolumeChannels [Name] [Volume Channel 1] [Volume Channel 2] ...
Set the sound volume of every channel separately. The [Volume Channel X]
is a number between 0 and 100, or '*' if you don't want to change the
volume of this channel.
You can use this command to set the audio balance of device or
application. If you have left and right channels, 0 is usually the left
channel and 1 is the right channel.

/SetVolumeChannelsDecibel [Name] [Volume Channel 1] [Volume Channel 2] ...
Only for subunit and device items. Set the sound volume of every channel
separately. The [Volume Channel X] is the volume in Decibels , or '*' if
you don't want to change the volume of this channel.

/ChangeVolume [Name] [Volume]
Changes the volume of the specified item. The [Volume] is a number
between -100 and 100, which specifies the percent of volume level to
increase (positive number) or decrease (negative number).

/ChangeVolumeChannel [Name] [Channel Number] [Volume]
Changes the channel volume of the specified item. In the [Channel Number]
parameter you should specify 0 for the first channel, 1 for the second
channel, and so on...
The [Volume] is a number between -100 and 100, which specifies the
percent of volume level to increase (positive number) or decrease
(negative number).
You can use this command to set the audio balance of device or
application. If you have left and right channels, 0 is usually the left
channel and 1 is the right channel.

/Mute [Name]
Mutes the volume of the specified item.

/Unmute [Name]
Unmutes the volume of the specified item.

/Disable [Name]
Disables the specified device

/Enable [Name]
Enables the specified device

/DisableEnable [Name]
Switches the specified device between active and disabled state.

/Switch [Name]
Switches the volume of the specified item between mute and unmute state.

/SetDefault [Name] [Default Type]
Sets the default device.
The [Default Type] parameter specifies one of the following values:
0 - Console
1 - Multimedia
2 - Communications
all - Set all default types (Console, Multimedia, and Communications)

/SwitchDefault [Name1] [Name2] [Default Type]
Switch between 2 default devices.
The [Default Type] parameter specifies one of the following values:
0 - Console
1 - Multimedia
2 - Communications
all - Set all default types (Console, Multimedia, and Communications)

/SetAppDefault [Name] [Default Type] [Process Name/ID]
Allows you to set the default render/capture device for specfic
application. This option is available only on Windows 10, starting from
Windows 10 April 2018 Update.
The [Name] parameter specifies the device name. If you want to set the
application back to the system default device, you should specify
DefaultCaptureDevice or DefaultRenderDevice as the device name.
The [Default Type] parameter specifies one of the following values:
0 - Console
1 - Multimedia
2 - Communications
all - Set all default types (Console, Multimedia, and Communications)
In the process paramater you can specify the process name (e.g:
firefox.exe) or process ID (e.g: 1524)

Examples:
SoundVolumeView.exe /SetAppDefault "High Definition Audio
Device\Device\Speakers\Render" 1 "chrome.exe"
SoundVolumeView.exe /SetAppDefault DefaultRenderDevice 0 firefox.exe
SoundVolumeView.exe /SetAppDefault "High Definition Audio
Device\Device\Speakers\Render" all vlc.exe

/SetDefaultFormat [Device Name] [Bits Per Sample] [Sample Rate - Hertz]
{Number Of Channels}
Sets the default format of the device. For example: The following command
sets the default format of the device to '24 bit, 192000 Hz(Studio
Quality)':
SoundVolumeView.exe /SetDefaultFormat "Realtek High Definition
Audio\Device\Speakers\Render" 24 192000

Another example: The following command sets the default format of the
device to '4 channel,24 bit, 48000 Hz(Studio Quality)':
SoundVolumeView.exe /SetDefaultFormat "Speakers" 24 48000 4

/SaveDeviceFormat [Device Name] [Filename]
Saves the data of the current default format of the specified device into
the specified filename. Later, You can load back the same format with the
/LoadDeviceFormat command.
You should use this command (together with /LoadDeviceFormat) if you have
formats that cannot be set with the /SetDefaultFormat command.

Example:
SoundVolumeView.exe /SaveDeviceFormat "Realtek High Definition
Audio\Device\Speakers\Render" "c:\temp\Speakers1.dat"

/LoadDeviceFormat [Device Name] [Filename]
Sets the default format of the device according to the format data you
previously saved with the /SaveDeviceFormat command. You can use this
command to set the default format if you cannot do it with the
/SetDefaultFormat command.

Example:
SoundVolumeView.exe /LoadDeviceFormat "Realtek High Definition
Audio\Device\Speakers\Render" "c:\temp\Speakers1.dat"

/SetSpeakersConfig [Device Name] [Channel Mask] [Optional Speakers] [Full
Range Speakers]
Sets the desired speakers configuration (Stereo, Quadraphonic, 5.1
Surround, 7.1 Surround, and so on..).
The [Channel Mask], [Optional Speakers] and [Full Range Speakers]
parameters are interger values that represent the speakers configuration.
You can use Hexadecimal numbers in these parameters by specifying '0x'
prefix.

You don't have to deeply understand the meaning of these values. Simply
set the desired speakers configuration from the user interface of
Windows, and then copy the 3 values from the 'Speakers Config' column.

Here's some examples:
Set the speakers configuration to Stereo (With all speakers selected) :
SoundVolumeView.exe /SetSpeakersConfig "Realtek High Definition
Audio\Device\Speakers\Render" 0x3 0x3 0x3


Set the speakers configuration to Quadraphonic (With all speakers
selected):
SoundVolumeView.exe /SetSpeakersConfig "Realtek High Definition
Audio\Device\Speakers\Render" 0x33 0x33 0x33


Set the speakers configuration to 5.1 Surround (With all speakers
selected):
SoundVolumeView.exe /SetSpeakersConfig "Realtek High Definition
Audio\Device\Speakers\Render" 0x3f 0x3f 0x3f

/SetSpatial [Device Name] [Spatial Sound Format]
Sets the 'Spatial sound format' of the specified sound device on Windows
10.
In the [Spatial Sound Format] parameter you can specify the full name of
the spatial sound format (e.g: Windows Sonic For Headphones), partial
name (e.g: Windows Sonic), or the GUID of the spatial sound format (e.g:
{b53d940c-b846-4831-9f76-d102b9b725a0} ).

If you want to disable the spatial sound format, simply use empty string
("")

Examples:
SoundVolumeView.exe /SetSpatial "High Definition Audio
Device\Device\Speakers\Render" "{b53d940c-b846-4831-9f76-d102b9b725a0}"
SoundVolumeView.exe /SetSpatial "High Definition Audio
Device\Device\Speakers\Render" "Windows Sonic For Headphones"
SoundVolumeView.exe /SetSpatial "High Definition Audio
Device\Device\Speakers\Render" "Windows Sonic"
SoundVolumeView.exe /SetSpatial "High Definition Audio
Device\Device\Speakers\Render" ""

/SetBooleanFxProperty [Device Name] [Property ID] [0 | 1 | 2]
Allows you to turn on/off different options of a device, like 'Loudness
Equalization', 'Headphone Virtualization', 'Voice Cancellation', and
others.

You can use one of the following values to turn on/off the specified
property:
0 - Turn Off
1 - Turn On
2 - Switch between on/off state.

Here's a list of some property IDs that you can use with this command:
Loudness Equalization - "{E0A941A0-88A2-4df5-8D6B-DD20BB06E8FB},4"
Headphone Virtualization - "{e0a941a0-88a2-4df5-8d6b-dd20bb06e8fb},3"
Voice Cancellation - "{4b361010-def7-43a1-a5dc-071d955b62f7},3"
Pitch Shift - "{9cc064e5-7fdc-4f03-9994-f24d4908aa26},1"
Equalizer - "{4b361010-def7-43a1-a5dc-071d955b62f7},5"
Environment - "{4b361010-def7-43a1-a5dc-071d955b62f7},1"
Virtual Surround - "{e0a941a0-88a2-4df5-8d6b-dd20bb06e8fb},2"

Examples:
SoundVolumeView.exe /SetBooleanFxProperty "Realtek High Definition
Audio\Device\Speakers\Render" "{E0A941A0-88A2-4df5-8D6B-DD20BB06E8FB},4" 1
SoundVolumeView.exe /SetBooleanFxProperty "Realtek High Definition
Audio\Device\Speakers\Render" "{e0a941a0-88a2-4df5-8d6b-dd20bb06e8fb},3" 2

/SetListenToThisDevice [Name] [0 | 1]
Sets the 'Listen to this device' value (For recording devices only).
0 = No, 1 = Yes.
Example: SoundVolumeView.exe /SetListenToThisDevice "Microphone" 1

/SetPlaybackThroughDevice [Recording Device] [Playback Device]
Sets the 'Playback through this device' value.
Example: SoundVolumeView.exe /SetPlaybackThroughDevice "Microphone"
"{0.0.0.00000000}.{7747b192-73b2-47d3-a2c0-168e94af7f9e}"

/SetAllowExclusive [Name] [0 | 1]
Sets the 'Allow applications to take exclusive control of this device'
option for the specified device.
0 = No, 1 = Yes.
Example: SoundVolumeView.exe /SetAllowExclusive "Realtek High Definition
Audio\Device\Speakers\Render" 1

/SetExclusivePriority [Name] [0 | 1]
Sets the 'Give exclusive mode applications priority' option for the
specified device.
0 = No, 1 = Yes.
Example: SoundVolumeView.exe /SetExclusivePriority "Realtek High
Definition Audio\Device\Speakers\Render" 1

/RunAsAdmin
Runs SoundVolumeView as administrator.

/WaitForItem [Number Of Seconds]
Instructs SoundVolumeView to wait the specified number of seconds until
the sound item appears. You can use this feature to change the
application volume for applications that are not running at this moment.
For example, if you want to mute the sound of Chrome Web browser and it's
not running at this moment:
SoundVolumeView.exe /Mute chrome.exe /WaitForItem 5000

In the above example, SoundVolumeView will wait up to 5000 seconds (The
process will remain in memory). If during the 5000 seconds period you run
the Chrome Web browser, SoundVolumeView will detect it, mute the sound of
Chrome application and then the process of SoundVolumeView will be
terminated. You can specify 0 in [Number Of Seconds] if you want to wait
infinitely.

/SaveProfile [Filename]
Saves the current sound settings into the specified profile filename.

/LoadProfile [Filename]
Restores the sound settings from the specified profile filename.

Here's some examples:
* SoundVolumeView.exe /GetPercent "Speakers"
* SoundVolumeView.exe /GetPercentChannel "Speakers" 0
* SoundVolumeView.exe /GetMute "Speakers"
* SoundVolumeView.exe /SetVolume
  "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}" 15
* SoundVolumeView.exe /SetVolume "Speakers" 22.5
* SoundVolumeView.exe /SetVolume "High Definition Audio
  Device\Device\Speakers" 50
* SoundVolumeView.exe /SetVolume "USB*\Device\Speakers\Render" 70
  /SetVolume "High Definition Audio Device\Device\Speakers" 35
* SoundVolumeView.exe /SetVolume AllAppVolume 100
* SoundVolumeView.exe /SetVolumeDecibel "Speakers" -11
* SoundVolumeView.exe /ChangeVolume "Front Green In" -10
* SoundVolumeView.exe /ChangeVolume
  "{2}.\\?\hdaudio#func_01&ven_10ec&dev_0889&subsys_1458a002&rev_1000#4&382
  8eb94&0&0201#{6994ad04-93ef-11d0-a3cc-00a0c9223196}\singlelineouttopo/000
  2000c&" 5
* SoundVolumeView.exe /ChangeVolumeChannel "vlc.exe" 0 -20
* SoundVolumeView.exe /Mute "singlelineouttopo/0002000c&"
* SoundVolumeView.exe /Mute "DefaultRenderDevice"
* SoundVolumeView.exe /Mute "System Sounds"
* SoundVolumeView.exe /SetVolumeChannels "Front Pink In" 45 55
* SoundVolumeView.exe /SetVolumeChannels "Front" * 60 * 40
* SoundVolumeView.exe /SetVolumeDecibel "Microphone Boost" 20
* SoundVolumeView.exe /ChangeVolumeDecibel "Microphone Boost" 10
* SoundVolumeView.exe /Mute "Firefox.exe"
* SoundVolumeView.exe /Mute "Firefox.exe" /WaitForItem 3600
* SoundVolumeView.exe /SetVolume "chrome.exe" 75
* SoundVolumeView.exe /SetVolumeChannels "chrome.exe" 50 100
* SoundVolumeView.exe /Unmute "VLC media player"
* SoundVolumeView.exe /Switch "C:\Program Files\Firefox.exe"
* SoundVolumeView.exe /Switch 3217
* SoundVolumeView.exe /Switch Focused
* SoundVolumeView.exe /Switch FocusedName
* SoundVolumeView.exe /Switch "Realtek High Definition
  Audio\Device\Speakers"
* SoundVolumeView.exe /Mute Curosr
* SoundVolumeView.exe /Mute CursorName
* SoundVolumeView.exe /DisableEnable Speakers
* SoundVolumeView.exe /SetDefault
  "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}" 0
* SoundVolumeView.exe /SetDefault
  "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}" all
* SoundVolumeView.exe /SetDefault "DENON-AVAMP" 1
* SoundVolumeView.exe /SwitchDefault
  "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}"
  "{0.0.0.00000000}.{7747b192-73b2-47d3-a2c0-168e94af7f9e}" all
* SoundVolumeView.exe /SetAppDefault
  "{0.0.0.00000000}.{a77a09b2-1ec6-49c3-860a-68945904a2f1}" all
  "myapp.exe"
* SoundVolumeView.exe /SetAppDefault DefaultCaptureDevice 1 1952
* SoundVolumeView.exe /SaveProfile "C:\Temp\Profile1.spr"
* SoundVolumeView.exe /LoadProfile "C:\Temp\Profile2.spr"

You can also use the following command-line parameters to export the list
of all sound items info a file:

/stext <Filename>
Save the list of all sound items into a regular text file.

/stab <Filename>
Save the list of all sound items into a tab-delimited text file.

/scomma <Filename>
Save the list of all sound items into a comma-delimited text file (csv).

/stabular <Filename>
Save the list of all sound items into a tabular text file.

/shtml <Filename>
Save the list of all sound items into HTML file (Horizontal).

/sverhtml <Filename>
Save the list of all sound items into HTML file (Vertical).

/sxml <Filename>
Save the list of all sound items into XML file.

/sjson <Filename>
Save the list of all sound items into JSON file.

/Columns <Comma Delimited Columns List>
Allows you to set the columns to display or the columns to export from
command-line. You have to specify the column names, delimited by comma,
for example:
SoundVolumeView.exe /scomma c:\temp\sound-items.csv /Columns
"Name,Command-Line Friendly ID,Volume Percent"

You can also specify the column names without space characters, for
example:
SoundVolumeView.exe /Columns "Name,Command-LineFriendlyID,VolumePercent"

/SaveFileEncoding [0 - 3]
Set the character encoding for the other save commands. 0 = Default, 1 =
ANSI, 2 = UTF-16, 3 = UTF-8.
For example:
SoundVolumeView.exe /SaveFileEncoding 2 /scomma "c:\temp\sound_items.csv"



Translating SoundVolumeView to other languages
==============================================

In order to translate SoundVolumeView to other language, follow the
instructions below:
1. Run SoundVolumeView with /savelangfile parameter:
   SoundVolumeView.exe /savelangfile
   A file named SoundVolumeView_lng.ini will be created in the folder of
   SoundVolumeView utility.
2. Open the created language file in Notepad or in any other text
   editor.
3. Translate all string entries to the desired language. Optionally,
   you can also add your name and/or a link to your Web site.
   (TranslatorName and TranslatorURL values) If you add this information,
   it'll be used in the 'About' window.
4. After you finish the translation, Run SoundVolumeView, and all
   translated strings will be loaded from the language file.
   If you want to run SoundVolumeView without the translation, simply
   rename the language file, or move it to another folder.



License
=======

This utility is released as freeware. You are allowed to freely
distribute this utility via floppy disk, CD-ROM, Internet, or in any
other way, as long as you don't charge anything for this and you don't
sell it or distribute it as a part of commercial product. If you
distribute this utility, you must include all files in the distribution
package, without any modification !



Disclaimer
==========

The software is provided "AS IS" without any warranty, either expressed
or implied, including, but not limited to, the implied warranties of
merchantability and fitness for a particular purpose. The author will not
be liable for any special, incidental, consequential or indirect damages
due to loss of data or any other reason.



Feedback
========

If you have any problem, suggestion, comment, or you found a bug in my
utility, you can send a message to nirsofer@yahoo.com
