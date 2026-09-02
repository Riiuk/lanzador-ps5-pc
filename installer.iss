; Instalador de Lanzador PS5. Se compila con Inno Setup 6:
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; o, mejor, con  .\build.ps1 -Instalador , que antes construye el .exe.

#define AppName        "Lanzador PS5"
#define AppVersion     "1.0.0"
#define AppPublisher   "Abel Santiago Fuentes"
#define AppURL         "https://github.com/Riiuk/lanzador-ps5-pc"
#define AppExe         "LanzadorPS5.exe"

[Setup]
AppId={{8B3A7C21-6D45-4E9F-A1B2-5C7D9E0F3A64}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=LanzadorPS5-{#AppVersion}-instalador
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Instalar en Archivos de programa exige elevacion.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=assets\ps5.ico
LicenseFile=LICENSE

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "escritorio"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"
Name: "inicio"; Description: "Iniciar el Lanzador PS5 con Windows"; GroupDescription: "Al encender el ordenador:"; Flags: unchecked

[Files]
Source: "dist\LanzadorPS5\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; --tray en todos: el programa se queda escuchando en la bandeja y abre la
; ventana cuando enciendes la PS5. Sin argumentos haria lo mismo, pero explicito
; se lee mejor desde las propiedades del acceso directo.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--tray"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--tray"; Tasks: escritorio

[Run]
; El autoarranque lo escribe EL PROGRAMA, no el instalador.
;
; Inno avisa de ello y tiene razon: la clave Run vive en HKCU, y un instalador
; elevado ve el HKCU del usuario que acepto el UAC, que no tiene por que ser
; quien va a usar el programa. Con runasoriginaluser esto corre como el usuario
; de verdad y la entrada acaba donde debe. Ademas asi hay UN SOLO sitio que
; gestione el autoarranque -autostart.py-, que es el que ya usa el menu de la
; bandeja.
Filename: "{app}\{#AppExe}"; Parameters: "--autostart-on"; Flags: runasoriginaluser runhidden waituntilterminated; Tasks: inicio
Filename: "{app}\{#AppExe}"; Parameters: "--tray"; Description: "Iniciar el Lanzador PS5 ahora"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
; Se quita la entrada ANTES de borrar los archivos, o quedaria un autoarranque
; apuntando a un ejecutable que ya no existe.
; En desinstalacion el flag no se llama runasoriginaluser sino runascurrentuser:
; hace lo mismo, ejecutar como el usuario que lanzo el proceso y no elevado.
Filename: "{app}\{#AppExe}"; Parameters: "--autostart-off"; Flags: runascurrentuser runhidden waituntilterminated skipifdoesntexist; RunOnceId: "quitarAutoarranque"

[UninstallDelete]
Type: files; Name: "{app}\defaults.json"

[Code]
var
  PaginaCapturas: TInputDirWizardPage;

// OJO con los comentarios de llaves en Pascal Script: NO anidan. Si dentro de
// un comentario { } se escribe el nombre de una constante entre llaves, la
// primera llave de cierre termina el comentario y el resto se interpreta como
// codigo. Por eso aqui se usan comentarios de linea.
//
// Inno no tiene constante para la carpeta Imagenes: la de Documentos existe,
// la de Imagenes no. Y el atajo evidente -perfil de usuario mas \Pictures- da
// una ruta EQUIVOCADA en cuanto la carpeta esta redirigida a otra unidad o a
// OneDrive, que es de lo mas normal. Comprobado en la maquina de desarrollo:
// alli Imagenes esta en D:\Pictures, mientras que C:\Users\<usuario>\Pictures
// existe pero no es la buena. El atajo no habria dado ningun error: solo
// habria sugerido en silencio la carpeta que no era.
//
// La ruta de verdad esta en el registro, ya expandida, en Shell Folders.
function CarpetaImagenes(): String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER,
       'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders',
       'My Pictures', Result) or (Result = '') then
    Result := ExpandConstant('{userprofile}\Pictures');
end;

procedure InitializeWizard;
begin
  { Segunda pregunta, justo despues de la carpeta de instalacion.

    Hace falta porque el programa se instala en Archivos de programa, que es de
    SOLO LECTURA para el usuario: guardar ahi las capturas es imposible. Antes
    iban a una carpeta junto al ejecutable, y eso solo funciona ejecutandolo
    desde su carpeta de desarrollo. }
  PaginaCapturas := CreateInputDirPage(wpSelectDir,
    'Carpeta para las capturas de pantalla',
    'Donde quieres que se guarden las capturas?',
    'Al pulsar P durante la partida se guarda una captura a resolucion completa.' + #13#10 +
    'Elige la carpeta donde quieres guardarlas y pulsa Siguiente.' + #13#10 + #13#10 +
    'Puedes cambiarla mas adelante en config.json.',
    False, '');
  PaginaCapturas.Add('');
  PaginaCapturas.Values[0] := AddBackslash(CarpetaImagenes()) + 'Screenshots PS5';
end;

function CarpetaCapturas(Param: String): String;
begin
  Result := PaginaCapturas.Values[0];
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Destino: String;
  Json: String;
begin
  if CurStep = ssPostInstall then
  begin
    Destino := PaginaCapturas.Values[0];
    if Destino <> '' then
    begin
      { Se crea ya, para que el usuario la vea donde dijo y no se lleve una
        sorpresa la primera vez que pulse P. }
      ForceDirectories(Destino);

      { Se escribe junto al ejecutable y no en la configuracion del usuario: el
        instalador corre elevado, y si el equipo tiene varios usuarios cada uno
        tiene su propio config.json. Asi la eleccion vale para todos, y quien
        quiera puede sobreescribirla en el suyo. Las barras se duplican porque
        esto es JSON. }
      StringChangeEx(Destino, '\', '\\', True);
      Json := '{' + #13#10 +
              '  "screenshots_dir": "' + Destino + '"' + #13#10 +
              '}' + #13#10;
      SaveStringToFile(ExpandConstant('{app}\defaults.json'), Json, False);
    end;
  end;
end;
