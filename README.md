# Chat Relay

Chat Relay sincroniza los chats locales que elijas de **Codex** y **Claude Code para VS Code** entre equipos Windows. Funciona desde la bandeja de notificaciones y usa una carpeta compartida, por ejemplo, una carpeta privada sincronizada por MEGA.

## Instalar

1. Descarga y extrae el ZIP de [Releases](https://github.com/cesarreynab/chat-relay/releases).
2. Abre PowerShell en la carpeta extraída. Necesitas Python 3.11 o posterior con Tcl/Tk y Python disponible en PATH.
3. Ejecuta:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1
```

El instalador crea un entorno virtual local, añade un acceso directo y abre **Configuración**. Chat Relay se inicia cuando abres sesión. Si no lo ves junto al reloj de Windows, revisa los iconos ocultos. Para actualizar, sal desde la bandeja antes de volver a ejecutar el instalador.

## Configurar dos equipos

1. En ambos equipos, haz clic derecho en el icono de Chat Relay y elige **Configuración**. En **Carpeta compartida**, selecciona la copia local de una misma carpeta privada sincronizada. Usa una carpeta dedicada fuera de los perfiles de los asistentes.
2. En **Raíz local de proyectos**, selecciona la carpeta que contiene tus proyectos en cada equipo. Las raíces pueden estar en unidades distintas. Por ejemplo, un chat de `C:\Projects\demo` aparecerá bajo `D:\Projects\demo` en el otro equipo. La carpeta del proyecto debe existir en ambos.
3. En el equipo de origen, marca los chats que quieras sincronizar y guarda la selección.
4. En el otro equipo, marca los chats cuando aparezcan como **Compartido** y guarda. Cierra VS Code y el asistente correspondiente para que Chat Relay aplique la importación. Cuando aparezca **Importado**, vuelve a abrir el asistente.

La selección inicial está vacía. Después de aceptar un chat en un equipo, sus próximas revisiones se importan automáticamente cuando se cierra el asistente. Chat Relay comprueba la carpeta cada 12 segundos. Los cambios en chats ya publicados se exportan al cerrar el asistente. El tiempo de transferencia depende del cliente que sincroniza la carpeta.

## Qué se sincroniza

| Asistente | Alcance de esta versión |
| --- | --- |
| Codex local | El historial del chat y su Goal, con el objetivo, el estado, el presupuesto y los contadores registrados por Codex. Compatible con perfiles inicializados que usan `state_5.sqlite`, `goals_1.sqlite` y `thread_history_1.sqlite`. Para inicializar un perfil, abre y completa un chat local. Usa la misma versión de Codex en los dos equipos. |
| Claude Code local | El historial principal de la sesión y los eventos nativos de `/goal`. La reanudación de un Goal activo se verificó con el ejecutable 2.1.329 incluido con la extensión de VS Code. Claude recupera la condición activa y reinicia sus referencias de tiempo, turnos y consumo de tokens al reanudar. [Documentación de Goals](https://code.claude.com/docs/en/goal#resume-with-an-active-goal) |

El traslado de archivos de proyecto, sesiones en la nube, checkpoints, archivos auxiliares y adjuntos externos queda fuera del alcance de esta versión. Usa la misma cuenta del asistente en ambos equipos.

Las revisiones se guardan en `ChatRelay-v1` dentro de tu carpeta compartida. Contienen los datos nativos del chat en ZIP, así que usa una carpeta privada a la que solo tengan acceso los equipos de confianza. La suma SHA-256 permite detectar copias incompletas. Si ambos equipos cambian el mismo chat desde una revisión compartida, Chat Relay muestra **Conflicto** y conserva las revisiones de ambos. Desmarcar un chat detiene su sincronización local. Las copias publicadas se conservan en la carpeta compartida.

## Desarrollo

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m chat_relay --settings
```

**English summary.** Chat Relay is a Windows tray application for opt-in, bidirectional synchronization of local Codex and Claude Code chat histories and Goals. It uses a private shared folder and supports different project roots on each device.
