import { innerRed, blueLight, greenLight, tabsSettings } from "../setup";

export const tabColorsMap = new Map();
tabColorsMap.set(tabsSettings.redTab.id, innerRed);
tabColorsMap.set(tabsSettings.greenTab.id, greenLight);
tabColorsMap.set(tabsSettings.blueTab.id, blueLight);
