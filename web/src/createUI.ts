import { createWebui } from "./createWebui";
import {
  telegramLink,
  githubLink,
  xLink,
  nrfmLink,
  tabsSettings,
  displaySettings,
  headerSettings,
  infosSettings,
  searchSettings,
  mainSettings,
  searchResultSettings,
  queueSettings,
  footerSettings,
  webUiSettings,
} from "./setup";
import { makeIconPath, openPopUp, setMargin0 } from "./utils";
import { createTabs } from "./createTabs";
import { createDisplay } from "./createDisplay";
import { createHeader } from "./createHeader";
import { createInfos } from "./createInfos";
import { createSearchbar } from "./createSearchbar";
import { createMain } from "./createMain";
import { createSearchResult } from "./createSearchResult";
import { createQueue } from "./createQueue";
import { createFooter } from "./createFooter.1";

export const createUI = (
  currentqueue: string[],
  root: HTMLElement,
  results?: string[]
) => {
  //// UI
  const body = root;
  const webui = createWebui(webUiSettings);
  const [tabs, tabEvents, setTabsHeights] = createTabs(tabsSettings);
  const [display, setDisplayColor] = createDisplay(displaySettings);
  const [header, setNowPlayingTrack] = createHeader(
    // fare funzione che recupera valore
    "NIRVANA - SMELLS LIKE TEEN SPIRIT",
    headerSettings
  );
  const [infos, setInfosOnClick, setInfosTextColor, setText] = createInfos(
    // fare funzione che recupera valore
    "21 SATS PER TRACK & PER DOWN VOTE",
    infosSettings
  );
  const [searchbar, setPlaceholderColor, inputValueCb, setIconSrc] =
    createSearchbar("red-search", searchSettings);
  const main = createMain(mainSettings);
  const [
    searchResults,
    searchResultCbSetters,
    setResultBackgroundColor,
    showResult,
  ] = createSearchResult(searchResultSettings(results));
  const [queue, votesCbs, setQueueBgColor, setVoteColor, setTracksInfoColor] =
    createQueue(currentqueue, queueSettings);
  const [footer, setFooterColor] = createFooter(
    [
      [makeIconPath("telegram-icon"), telegramLink],
      [makeIconPath("github-cion"), githubLink],
      [makeIconPath("x-icon"), xLink],
      [makeIconPath("nrfm-icon"), nrfmLink],
    ],
    footerSettings
  );
  const activeTab = "red-tab";
  setMargin0(body);
  setDisplayColor(activeTab);
  setFooterColor(activeTab);
  setPlaceholderColor(activeTab);
  setTabsHeights(activeTab);

  webui.appendChild(tabs);
  webui.appendChild(display);
  display.appendChild(header);
  display.appendChild(infos);
  display.appendChild(searchbar);
  display.appendChild(main);
  main.appendChild(searchResults);
  main.appendChild(queue);
  webui.appendChild(footer);
  const setActive = (tab: string) => {
    setDisplayColor(tab);
    setFooterColor(tab);
    setIconSrc(tab);
    setPlaceholderColor(tab);
    setInfosTextColor(tab);
    setResultBackgroundColor(tab);
    setQueueBgColor(tab);
    setVoteColor(tab);
    setTracksInfoColor(tab);
    setTabsHeights(tab);
  };
  tabEvents.forEach((es) =>
    es((e) => {
      const element = e.target as HTMLElement;
      setActive(element.id);
    })
  );
  const searchResultCb = (cb: (e: MouseEvent) => void) => {
    if (searchResultCbSetters.length !== 0)
      searchResultCbSetters.forEach((e) => e(cb));
  };
  const setVotesCb = (
    down: (e: MouseEvent) => void,
    up: (e: MouseEvent) => void
  ) => {
    const downs = votesCbs[0];
    const ups = votesCbs[1];
    downs.forEach((d) => d(down));
    ups.forEach((d) => d(up));
  };
  setInfosOnClick(async (e) => {
    openPopUp(
      "http://127.0.0.1:5500/web/popup.html",
      "_blank",
      "width=286, height=466"
    );
  });
  return [
    webui,
    inputValueCb,
    searchResultCb,
    setVotesCb,
    showResult,
    setNowPlayingTrack,
    setText,
  ] as [
    HTMLElement,
    (cb: (inputvalue: string) => void) => void,
    (cb: (e: MouseEvent) => void) => void,
    (down: (e: MouseEvent) => void, up: (e: MouseEvent) => void) => void,
    resultSetter,
    trackCb,
    textCb
  ];
};
