-- BioKids (D-163): every ordinary app window fills the screen and keeps its
-- title bar, so the big close button is always in the same place - the top
-- right corner of the screen - whichever app a child has open.
--
-- The launcher's own window (WM_CLASS biokids-kiosk) is a full-screen kiosk
-- and is left alone. So are dialogs, menus and splash screens: they are not
-- WINDOW_TYPE_NORMAL, and a "save your drawing?" question should stay the size
-- of a question.
--
-- devilspie2 runs this once for each window as it appears. An app that then
-- puts back the size it saved last time, as KDE's apps do, is maximised again
-- by the app bar (D-205, appbar.Filler). It is started by the launcher with
-- --folder pointing here; nothing else reads this file.
if get_window_type() == "WINDOW_TYPE_NORMAL"
   and get_window_class() ~= "biokids-kiosk" then
  maximize()
end
