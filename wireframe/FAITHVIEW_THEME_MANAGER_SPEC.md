# FaithView Pro — Theme Manager Implementation Specification

## 1. Objective

Build a production-ready **Theme Manager** inside FaithView Pro, inspired by the supplied Canva-style mockup.

The Theme Manager must let a church create, edit, save, duplicate, import and export presentation themes. Themes must work offline on the device and synchronize with the authenticated user's cloud account.

The Theme Manager must also be accessible **during a live service**. If a verse is currently being projected, the operator can click that verse and open the Theme Manager editor for the exact projected verse without losing the live-service context.

---

## 2. Main location

Add:

**Settings → Theme Manager**

The Theme Manager should open as a dedicated editor.

Recommended layout:

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ FaithView Pro | Theme name | Synced | Import | Export | Save                 │
├──────┬───────────────────┬───────────────────────────────┬───────────────────┤
│      │                   │                               │                   │
│ Rail │ Context panel     │       Live slide canvas      │ Property editor   │
│      │                   │                               │                   │
│Theme │ Themes            │       1920 × 1080            │ Selected element  │
│Text  │ Scripture         │                               │ properties        │
│Bible │ Background        │   Verse                       │                   │
│BG    │ Uploads           │   Reference                   │                   │
│Media │ Alerts            │   Logo                        │                   │
│Alert │ Brand             │   Copyright                   │                   │
│Brand │                   │                               │                   │
└──────┴───────────────────┴───────────────────────────────┴───────────────────┘
```

The supplied HTML mockup is a visual reference and should be improved rather than copied blindly.

---

# 3. Theme model

A theme is a JSON document.

Suggested structure:

```json
{
  "id": "uuid",
  "name": "Selah Main Theme",
  "version": 1,
  "createdAt": "ISO_DATE",
  "updatedAt": "ISO_DATE",
  "ownerId": "USER_ID",
  "canvas": {
    "width": 1920,
    "height": 1080,
    "background": {
      "type": "color",
      "color": "#2B2470",
      "assetId": null,
      "fit": "cover",
      "position": {
        "x": 0,
        "y": 0
      },
      "scale": 1,
      "opacity": 1,
      "filter": "none",
      "video": {
        "playbackRate": 1,
        "muted": true,
        "loop": true
      }
    }
  },
  "elements": [
    {
      "id": "uuid",
      "type": "scripture",
      "content": {
        "text": "In the beginning God created the heaven and the earth",
        "reference": "Genesis 1:1",
        "translation": "NKJV"
      },
      "position": {
        "x": 960,
        "y": 350
      },
      "size": {
        "width": 1500,
        "height": 300
      },
      "style": {
        "fontFamily": "Fraunces",
        "fontSize": 52,
        "fontWeight": 600,
        "letterSpacing": 0,
        "lineHeight": 1.25,
        "color": "#FFFFFF",
        "textAlign": "center",
        "verticalAlign": "middle",
        "opacity": 1
      }
    },
    {
      "id": "uuid",
      "type": "reference",
      "content": {
        "text": "Genesis 1:1 (NKJV)"
      },
      "position": {
        "x": 960,
        "y": 700
      }
    },
    {
      "id": "uuid",
      "type": "copyright",
      "content": {
        "text": "Christ's Heart Church"
      },
      "position": {
        "x": 40,
        "y": 1030
      }
    },
    {
      "id": "uuid",
      "type": "logo",
      "assetId": "asset_uuid",
      "position": {
        "x": 1800,
        "y": 70
      },
      "size": {
        "width": 100,
        "height": 100
      },
      "opacity": 0.9
    }
  ],
  "alerts": {
    "enabled": true,
    "text": "",
    "direction": "left",
    "speed": 5,
    "fontFamily": "Inter",
    "fontSize": 32,
    "fontWeight": 700,
    "textColor": "#FFFFFF",
    "backgroundColor": "#8B6BFF",
    "showOnAllOutputs": true,
    "autoDismissSeconds": 30
  }
}
```

Use a schema/type system in the application so malformed themes cannot break the editor.

---

# 4. Theme persistence

## Local storage

Every theme should have a local copy.

Recommended options:

- IndexedDB for full theme/media metadata
- localStorage only for lightweight preferences and the active theme ID
- Cache Storage/service worker if the application is PWA/offline-capable

Do not store large videos directly in localStorage.

Themes should remain usable when internet connectivity disappears.

## Cloud synchronization

Authenticated users should have themes synchronized to their account.

Recommended behavior:

1. User edits theme.
2. Changes are reflected immediately in local state.
3. Debounced autosave writes the theme locally.
4. Cloud synchronization happens in the background.
5. UI displays:
   - `Saving…`
   - `Saved locally`
   - `Synced`
   - `Offline`
   - `Sync failed`
6. If offline, queue changes for later synchronization.
7. On reconnect, synchronize queued changes.

Do not make the UI freeze while waiting for cloud synchronization.

---

# 5. Import/export

The user must be able to export a theme.

Button:

**Export .thmpkg**

A theme package should contain:

```text
theme.thmpkg
├── theme.json
└── assets/
    ├── logo-1.png
    ├── background-1.jpg
    └── background-video.mp4
```

For a first implementation, a `.json` export is acceptable:

`faithview-theme.thmpkg.json`

However, design the service layer so it can later produce a ZIP-based `.thmpkg`.

Import must:

- validate JSON/schema
- detect missing assets
- avoid crashing on unknown fields
- show a useful error if the package is invalid
- allow importing as a new theme rather than silently overwriting the current theme

---

# 6. Canvas/editor

The editor should feel like a lightweight Canva-style presentation editor.

The canvas represents the real output resolution:

**1920 × 1080**

The user can:

- click an element
- drag it
- resize it
- duplicate it
- delete it
- align it
- move it independently
- lock/unlock it
- edit its properties from the right panel

Use a proper coordinate system based on the 1920×1080 canvas rather than storing positions based on browser pixels.

Recommended:

```text
position.x
position.y
size.width
size.height
rotation
scale
```

The canvas may visually scale down to fit the browser, but element coordinates must remain resolution-independent.

---

# 7. Scripture element

Scripture is a first-class element.

The following should be independently movable:

1. Verse text
2. Reference

Example:

```text
In the beginning God created the heaven and the earth

Genesis 1:1 (NKJV)
```

The verse and reference must NOT be permanently grouped.

The user should be able to move:

- verse to the center
- reference below it
- reference above it
- verse left/right
- both independently

## Scripture creation

The Scripture panel should have:

```text
Search scripture
[ Genesis 1:1                 ]

Translation
[ NKJV ▼ ]

[ Add verse ]
[ Add reference ]
```

Supported translations should be determined by the application's existing Bible/licensing system.

Do not hardcode copyrighted Bible text into the Theme Manager itself.

---

# 8. Text editing panel

When the user clicks a verse, reference or copyright element, open the right-side property editor.

### Color

Provide:

- preset color swatches
- HEX input
- RGB controls
- opacity/alpha

Example:

```text
Color

● ● ● ● ●

#FFFFFF

R 255
G 255
B 255
A 100%
```

### Alignment

Provide icon buttons:

```text
[ Left ] [ Center ] [ Right ]

[ Top ] [ Middle ] [ Bottom ]
```

### Typography

Include:

- font family
- font size
- font weight
- letter spacing
- line height
- text alignment
- opacity

Font weight should use a slider:

```text
100 ─────────────●──────── 900
                 600
```

Font size should have:

```text
[ − ] 44 [ + ]
```

Font family should include major families such as:

- Inter
- Roboto
- Arial
- Helvetica
- Open Sans
- Poppins
- Montserrat
- Lato
- Nunito
- Raleway
- Work Sans
- Georgia
- Times New Roman
- Merriweather
- Playfair Display
- Lora
- Libre Baskerville
- Source Serif
- Cormorant Garamond
- EB Garamond
- Fraunces

Only show fonts that are actually available/loaded in the production application.

### Content

For normal text elements provide a textarea.

For scripture elements, content should be linked to the scripture selection system where appropriate.

---

# 9. Copyright/church name element

The church name/copyright should be an independent movable element.

Example:

```text
Christ's Heart Church
```

It must support:

- position
- size
- color
- font
- weight
- letter spacing
- line height
- opacity
- alignment
- resize

It should not be permanently attached to the canvas corner.

---

# 10. Background system

Background is treated as a special canvas layer.

Types:

1. Solid color
2. Image
3. Video

UI:

```text
Background

[ Color ] [ Image ] [ Video ]
```

## Solid color

Allow:

- preset colors
- HEX
- RGB
- opacity

Example presets:

- purple
- green
- dark red
- navy
- black
- gold

The user can enter any custom color.

---

# 11. Image background

Allow selecting an image from:

### Existing media library

```text
Media Library

[ worship.jpg ] [ stage.jpg ]
[ clouds.jpg  ] [ mountain.jpg ]
```

### Computer

Button:

**Upload from device**

Supported image formats should include common browser-supported formats.

After selecting an image, allow:

- Cover
- Contain
- Stretch
- Crop
- scale/zoom
- X position
- Y position
- opacity/transparency
- filters

The image itself should be repositionable with mouse dragging when the background editor is active.

---

# 12. Video background

Allow:

**Upload video**

or selecting an existing video from the media library.

Controls:

- playback speed
- volume
- mute
- loop
- opacity
- scale
- position
- fit mode
- filters

Example:

```text
Playback speed
0.25x ─────●────── 2x

Sound
[ OFF ]

Loop
[ ON ]
```

For live-service reliability, video should be preloaded/cached where possible.

The system should gracefully fall back to a static poster frame if the video cannot load.

---

# 13. Background filters

Provide built-in filters:

- None
- Warm
- Cool
- Vintage
- Purple
- Mono
- Dark
- Bright
- High Contrast

Filters should be implemented with CSS filters or a rendering layer.

Do not require users to download filters individually.

---

# 14. Uploads / media library

Create an **Uploads** section similar to Canva.

Users can upload:

- JPG
- PNG
- WEBP
- SVG where supported
- MP4
- WebM where supported
- other safe browser-supported formats

Every media item should have:

```text
assetId
ownerId
fileName
mimeType
size
width
height
duration
thumbnail
storageUrl
createdAt
```

The same asset can be used as:

- logo
- background
- normal image element
- alert artwork in the future

Do not duplicate the actual file when the same asset is reused.

---

# 15. Logo image element

A logo must be independent of the background.

The user can:

1. Open Uploads.
2. Select an image.
3. Click **Add as logo**.

The logo becomes a movable canvas element.

Controls:

- width
- height
- proportional resize
- X
- Y
- opacity
- rotation
- lock
- delete
- replace image

The user should be able to drag the logo directly around the canvas.

---

# 16. Canva-style direct manipulation

The editor should support direct manipulation rather than requiring every operation to happen in the property panel.

When selected:

```text
┌──────────────────────────────┐
│         selected box         │
│   ●                      ●   │
│                              │
│   ●                      ●   │
└──────────────────────────────┘
```

Corners should allow resizing.

Dragging inside moves the element.

Toolbar:

```text
Align left | Center | Right | Duplicate | Lock | Delete
```

Keyboard shortcuts:

- Delete → delete selected
- Ctrl/Cmd + D → duplicate
- Ctrl/Cmd + Z → undo
- Ctrl/Cmd + Shift + Z → redo
- Arrow keys → move selected element
- Shift + Arrow → move faster
- Escape → deselect

Implement undo/redo as a command/history system rather than manually patching individual UI controls.

---

# 17. Nursery alerts

Add a dedicated left-rail item:

**Alerts**

Opening it should show an alert designer.

Purpose:

Display TV-style scrolling lower-third messages during a live church service.

Example:

```text
┌─────────────────────────────────────────────┐
│ Parents of child #114, please check nursery │
└─────────────────────────────────────────────┘
```

Text should continuously scroll horizontally.

---

# 18. Alert controls

### Message

Textarea:

```text
[ Parents of child #114, please check the nursery ]
```

### Direction

```text
[ ← ] [ → ] [ ↑ ]
```

Primary/default direction should be left.

### Speed

Slider:

```text
Slow ─────────●──────── Fast
```

Use a numeric internal value so it can be stored in JSON.

### Typography

Controls:

- font family
- font size
- font weight
- letter spacing
- text color

### Background

Controls:

- background color
- opacity
- optional border
- optional rounded corners

### Behavior

Controls:

- show on all outputs
- loop
- auto-dismiss
- duration

Button:

**Push alert live**

---

# 19. Live-service integration

This is important.

While running a live service, the currently projected scripture should be aware of its theme.

Example:

```text
LIVE SERVICE

Genesis 1:1 (NKJV)
        ↓
click projected verse
        ↓
Theme Manager opens
        ↓
exact verse element is selected
        ↓
operator changes font/color/position
        ↓
preview updates
        ↓
live output updates
```

Do NOT create a separate copy of the verse that can become out of sync.

The live-service view and Theme Manager should reference the same presentation/theme state.

Recommended architecture:

```text
Live Service
     │
     ▼
Presentation State
     │
     ├── Current slide
     ├── Current scripture
     └── Active theme
              │
              ▼
        Theme Manager
              │
              ▼
       Updated element
              │
              ▼
        Output renderer
```

Changes made during live service should be reflected in the projected output safely.

If desired, add:

**Apply to current slide**

and

**Save to theme**

so a one-off live change does not accidentally alter the global theme.

---

# 20. Important distinction: theme vs slide override

Implement two concepts.

## Theme

Global design configuration:

- fonts
- default colors
- default background
- logo
- copyright
- default scripture styling
- alert styling

## Slide override

A specific slide can temporarily override the theme.

Example:

```text
Theme:
Verse font = Fraunces 52px

Current slide override:
Verse font = Inter 60px
```

This prevents an operator from accidentally changing every future scripture slide while editing one live slide.

UI can show:

```text
Current slide
[ Apply change to this slide ]

Theme
[ Save as theme default ]
```

---

# 21. Theme management

Themes screen should provide:

- New theme
- Duplicate theme
- Rename theme
- Delete theme
- Set as default
- Export
- Import
- Cloud sync status
- Local/offline status

Theme cards should show a visual thumbnail.

Example:

```text
Selah
Main Theme
Synced

Eden
Youth Theme
Synced

Christ's Heart Default
Local draft
```

---

# 22. Autosave

Do not make the user press Save for every small change.

Recommended:

- update UI immediately
- debounce local persistence ~300–1000ms
- cloud sync after local persistence
- explicit **Save** button remains available

Top bar:

```text
● Saved locally · Synced
```

During synchronization:

```text
◌ Saving…
```

When offline:

```text
● Offline · Saved on device
```

---

# 23. Asset storage architecture

Do not put large media files inside the theme JSON.

Use:

```text
Theme JSON
    ↓
assetId
    ↓
Media Library
    ↓
actual file
```

For example:

```json
{
  "type": "logo",
  "assetId": "asset_123"
}
```

The media library owns the actual file.

Cloud storage can use the project's existing backend/storage system.

---

# 24. Security

Users must only be able to access their own:

- themes
- assets
- cloud files

Do not trust `ownerId` sent by the browser.

The backend should derive the authenticated user identity from the session/token.

Validate:

- file type
- file size
- upload permissions
- theme JSON
- asset references

Never execute uploaded SVG/scripts as arbitrary HTML.

---

# 25. UI behavior requirements

The UI must actually work, not merely look good.

Required interactions:

- left navigation switches panels
- selecting canvas elements opens their property panel
- clicking background opens background editor
- clicking logo opens logo editor
- dragging works
- resizing works
- text changes update canvas immediately
- font controls update canvas
- color controls update canvas
- background presets work
- image selection works
- upload input works
- alert preview animates
- alert settings update the preview
- save stores theme
- reload restores theme
- export downloads theme
- import validates and loads theme
- undo/redo works
- live-service entry point selects the correct element

Do not leave placeholder buttons that look functional but do nothing.

---

# 26. Suggested frontend architecture

Do not implement the entire editor as one giant HTML file in production.

Separate:

```text
theme-manager/
├── components/
│   ├── ThemeManager
│   ├── ThemeRail
│   ├── ThemeList
│   ├── ThemeCanvas
│   ├── CanvasToolbar
│   ├── ElementRenderer
│   ├── PropertyPanel
│   ├── ScriptureEditor
│   ├── BackgroundEditor
│   ├── LogoEditor
│   ├── UploadsPanel
│   ├── AlertsEditor
│   └── ColorPicker
├── state/
│   ├── themeStore
│   ├── historyStore
│   └── liveServiceStore
├── services/
│   ├── themeStorage
│   ├── themeSync
│   ├── mediaService
│   ├── importExport
│   └── livePresentationService
├── types/
│   └── theme.ts
└── utils/
    ├── coordinates
    ├── validation
    └── filters
```

Use the project's existing framework and state-management conventions rather than introducing another framework unnecessarily.

---

# 27. Rendering recommendation

The editor can use normal HTML/CSS positioned elements initially.

For example:

```text
Canvas
 ├── Background layer
 ├── Video layer
 ├── Image/logo layers
 ├── Scripture layer
 ├── Reference layer
 ├── Copyright layer
 └── Alert overlay
```

Keep the document model independent from the DOM.

The DOM should be a rendering of the theme state.

This is important for:

- saving
- undo/redo
- cloud sync
- live output
- importing/exporting
- responsive canvas scaling

---

# 28. Acceptance criteria

The implementation is complete only when all of the following work:

- [ ] User can open Theme Manager from Settings.
- [ ] User can create a new theme.
- [ ] User can rename a theme.
- [ ] User can duplicate a theme.
- [ ] User can delete a theme.
- [ ] User can select an existing theme.
- [ ] User can edit verse text.
- [ ] User can edit reference text.
- [ ] Verse and reference can move independently.
- [ ] Copyright/church name is independently movable.
- [ ] Logo is independently movable.
- [ ] Logo can be resized.
- [ ] Logo transparency can be changed.
- [ ] User can choose a plain background color.
- [ ] User can enter HEX color.
- [ ] User can edit RGB values.
- [ ] User can choose background images from media library.
- [ ] User can upload background images.
- [ ] User can upload/select videos.
- [ ] Video speed can be changed.
- [ ] Video sound can be toggled.
- [ ] Video loop can be enabled.
- [ ] Background opacity can be changed.
- [ ] Background scale/position can be changed.
- [ ] Background filters work.
- [ ] Font family works.
- [ ] Font size works.
- [ ] Font weight slider works.
- [ ] Letter spacing works.
- [ ] Line height works.
- [ ] Text color works.
- [ ] Alignment works.
- [ ] Uploads have a reusable media library.
- [ ] Nursery alerts can be configured.
- [ ] Alert text can be edited.
- [ ] Alert speed can be changed.
- [ ] Alert font can be changed.
- [ ] Alert size can be changed.
- [ ] Alert weight can be changed.
- [ ] Alert text color can be changed.
- [ ] Alert background color can be changed.
- [ ] Alert preview scrolls.
- [ ] Alert can be pushed live.
- [ ] Theme saves locally.
- [ ] Theme restores after reload.
- [ ] Theme syncs to cloud.
- [ ] Offline edits are queued.
- [ ] Theme can be exported.
- [ ] Theme can be imported.
- [ ] Invalid imports are rejected safely.
- [ ] Undo works.
- [ ] Redo works.
- [ ] Live-service verse can open the Theme Manager.
- [ ] The exact currently projected element is selected.
- [ ] Live changes update the correct presentation state.
- [ ] A one-off slide override cannot accidentally overwrite the global theme.
- [ ] UI has no dead/placeholder controls in the final implementation.

---

# 29. Design direction

Use the supplied mockup as the visual starting point:

- dark professional interface
- left tool rail
- contextual left panel
- large central canvas
- right property inspector
- purple primary accent
- subtle borders
- compact controls
- rounded panels
- strong typography
- Canva-like direct manipulation

However, improve it where needed for actual usability.

The final product should feel like a **professional church presentation theme editor**, not a generic graphics editor.

Keep the UI visually clean and avoid overwhelming the operator during a live service.

---

# 30. Final implementation instruction for Claude Code

First inspect the existing FaithView Pro project.

Do NOT rewrite unrelated parts of the application.

Before coding:

1. Identify the current frontend framework.
2. Identify routing.
3. Identify state management.
4. Identify authentication.
5. Identify existing presentation/live-service state.
6. Identify existing backend/database.
7. Identify existing file/media storage.
8. Identify existing scripture/Bible data services.
9. Identify existing UI component library.
10. Reuse existing infrastructure wherever possible.

Then implement Theme Manager incrementally.

Suggested order:

### Phase 1
Theme data model + local persistence.

### Phase 2
Canvas and movable/resizable elements.

### Phase 3
Right-side typography/color property editor.

### Phase 4
Background system.

### Phase 5
Media/uploads and logo system.

### Phase 6
Nursery alerts.

### Phase 7
Cloud synchronization.

### Phase 8
Import/export.

### Phase 9
Live-service integration.

### Phase 10
Undo/redo, polish, testing and accessibility.

Do not stop at a static UI mockup.

Every visible control should either work or be intentionally disabled with a clear reason.

The existing supplied HTML mockup should be treated as the design reference for the editor, while the production implementation should use FaithView Pro's existing architecture and components.
