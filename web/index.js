// web/src/setup.ts
var vote = true;
var innerRed = `#ff8a8d`;
var darkRed = `#c50200`;
var superDark = `#720000`;
var greenLight = `#a5f782`;
var greencolor = `#73d04c`;
var greenDark = `#27821a`;
var blueLight = `#8ba5f6`;
var bluecolor = `#5e84fc`;
var blueDark = `#0d459d`;
var fontMid = `"1.3rem"`;
var tabwidth = 6;
var tabHeight = 7;
var footerHeight = 7;
var gridTemplate = "1fr 5fr 1fr";
var tabIconsSize = 32;
var medSection = 6;
var bigSection = 9;
var spaces = [medSection, medSection, bigSection];

// web/src/utils.ts
var openPopUp = (url, target, features) => window.open(url, target, features);
var setMargin = (margin) => (element) => element.style.margin = margin;
var setHeight = (height) => (element) => element.style.height = height;
var setDisplay = (display) => (element) => element.style.display = display;
var setBorderRadius = (radius) => (element) => element.style.borderRadius = radius;
var setMargin0 = setMargin("0");
var setMargin1rem = setMargin("1rem");
var setHeight6cqh = setHeight(`${medSection}cqh`);
var setHeight9cqh = setHeight(`${bigSection}cqh`);
var setHeight91cqh = setHeight(`${100 - spaces.reduce((p, c) => p + c)}cqh`);
var setDisplayFlex = setDisplay("flex");
var setDisplayGrid = setDisplay("grid");
var setElementStyle = (element, text) => {
  setMargin("3px")(element);
  setBorderRadius("4px")(element);
  text.style.padding = "0.3rem";
  text.style.color = "black";
};
var createDiv = () => document.createElement("div");
var createSvg = (src, height = "1rem") => {
  const svg = document.createElement("img");
  svg.setAttribute("src", src);
  setHeight(height)(svg);
  return svg;
};
var createP = () => {
  const p = document.createElement("p");
  setMargin0(p);
  return p;
};
var createButton = (image) => {
  const button = document.createElement("button");
  button.style.border = "none";
  button.style.backgroundColor = "white";
  button.style.borderRadius = "4px";
  const svg = createSvg(image);
  svg.style.height = "1.3rem";
  button.appendChild(svg);
  return button;
};
var makeIconPath = (icon) => `../Assets/icons/${icon}.svg`;
var makeLinearGradient = (colora, colorb) => `linear-gradient(180deg,${colora},${colorb})`;

// web/src/index.ts
var getResults = () => {
  return new Array(25).fill("EMPTY");
};
var getQueue = () => {
  return new Array(25).fill("I WILL BE THERE");
};
var createUI = (currentqueue, results) => {
  const map = new Map;
  map.set("red-tab", innerRed);
  map.set("green-tab", greenLight);
  map.set("blue-tab", blueLight);
  const setElementColor = (element) => (color) => {
    element.style.color = map.get(color);
  };
  const setElementBgColor = (element) => (color) => {
    element.style.backgroundColor = map.get(color);
  };
  const redGradient = makeLinearGradient(darkRed, superDark);
  const alldarkred = makeLinearGradient(superDark, superDark);
  const greenGradient = makeLinearGradient(greencolor, greenDark);
  const alldarkgreen = makeLinearGradient(greenDark, greenDark);
  const blueGradient = makeLinearGradient(bluecolor, blueDark);
  const alldarkblue = makeLinearGradient(blueDark, blueDark);
  const gradientMap = new Map;
  gradientMap.set("red-tab", [redGradient, alldarkred]);
  gradientMap.set("red-tab-icon", [redGradient, alldarkred]);
  gradientMap.set("green-tab", [greenGradient, alldarkgreen]);
  gradientMap.set("green-tab-icon", [greenGradient, alldarkgreen]);
  gradientMap.set("blue-tab", [blueGradient, alldarkblue]);
  gradientMap.set("blue-tab-icon", [blueGradient, alldarkblue]);
  const iconpaths = new Map;
  iconpaths.set("red-tab", makeIconPath("red-search"));
  iconpaths.set("red-tab-icon", makeIconPath("red-search"));
  iconpaths.set("green-tab", makeIconPath("green-search"));
  iconpaths.set("green-tab-icon", makeIconPath("green-search"));
  iconpaths.set("blue-tab", makeIconPath("blue-search"));
  iconpaths.set("blue-tab-icon", makeIconPath("blue-search"));
  const placeholderColors = new Map;
  placeholderColors.set("red-tab", darkRed);
  placeholderColors.set("red-tab-icon", darkRed);
  placeholderColors.set("green-tab", greencolor);
  placeholderColors.set("green-tab-icon", greencolor);
  placeholderColors.set("blue-tab", bluecolor);
  placeholderColors.set("blue-tab-icon", bluecolor);
  const createWebui = () => {
    const container = createDiv();
    container.id = "webui";
    const setWebUIStyle = () => {
      const style = container.style;
      style.height = "100vh";
      style.containerType = "size ";
      style.fontFamily = "boucherie-block";
      style.color = "white";
    };
    setWebUIStyle();
    return container;
  };
  const createTabs = () => {
    const container = createDiv();
    container.id = "tabs-area";
    container.style.position = "relative";
    setDisplayFlex(container);
    container.style.height = tabHeight + "cqh";
    const red = createDiv();
    red.id = "red-tab";
    const redsvg = createSvg(makeIconPath("Red-tab-icon"), `${tabIconsSize}px`);
    redsvg.id = "red-tab-icon";
    red.appendChild(redsvg);
    red.style.backgroundColor = darkRed;
    const green = createDiv();
    green.id = "green-tab";
    const greenp = createP();
    const greensvg = createSvg(makeIconPath("green-tab-icon"), `${tabIconsSize}px`);
    green.appendChild(greensvg);
    greensvg.id = "green-tab-icon";
    const gr = greencolor;
    greenp.textContent = gr;
    green.style.backgroundColor = gr;
    green.style.left = tabwidth + `rem`;
    const blue = createDiv();
    blue.id = "blue-tab";
    const bluesvg = createSvg(makeIconPath("blue-tab-icon"), `${tabIconsSize}px`);
    bluesvg.id = "blue-tab-icon";
    blue.appendChild(bluesvg);
    blue.style.backgroundColor = bluecolor;
    blue.style.left = tabwidth * 2 + `rem`;
    setHeight("20px")(red);
    setHeight("20px")(green);
    setHeight("20px")(blue);
    const setTabStyle = (tab, active = true) => {
      const style = tab.style;
      style.width = tabwidth + `rem`;
      style.position = "absolute";
      style.bottom = "0";
      style.height = active ? "100%" : "60%";
      style.borderRadius = "5px 5px 0 0";
      style.display = "grid";
      style.placeItems = "center";
      style.transition = "height 0.5s ease";
    };
    const setRedCb = (cb) => {
      red.onclick = cb;
    };
    const setGreebCb = (cb) => {
      green.onclick = cb;
    };
    const setBlueCb = (cb) => {
      blue.onclick = cb;
    };
    const setTabsHeights2 = (color) => {
      [red, green, blue].forEach((e) => {
        if (e.id === color) {
          console.log(e.id);
          setTabStyle(e);
        } else {
          setTabStyle(e, false);
        }
      });
    };
    [red, green, blue].forEach((e) => setTabStyle(e));
    container.appendChild(red);
    container.appendChild(green);
    container.appendChild(blue);
    return [container, [setRedCb, setGreebCb, setBlueCb], setTabsHeights2];
  };
  const createDisplay = () => {
    const container = createDiv();
    container.id = "display-area";
    container.style.height = 100 - [tabHeight, footerHeight].reduce((p, c) => p + c) + "cqh";
    container.style.containerType = "size";
    const setColor = (color) => container.style.backgroundImage = gradientMap.get(color);
    return [container, setColor];
  };
  const createHeader = (songinfostext) => {
    const container = createDiv();
    container.style.placeItems = "center";
    container.style.placeContent = "space-evenly";
    container.id = "jb-header";
    setHeight9cqh(container);
    const nowPlaying = createP();
    nowPlaying.textContent = "NOW PLAYING: " + songinfostext;
    const setNowPlayingStyle = () => {
      const style = nowPlaying.style;
      const margintb = "0.3rem";
      const marginlr = "0.2rem";
      style.paddingTop = margintb;
      style.paddingRight = marginlr;
      style.paddingBottom = margintb;
      style.paddingLeft = marginlr;
      style.borderTop = "solid 1px white";
      style.borderBottom = "solid 1px white";
      style.fontSize = fontMid;
    };
    setNowPlayingStyle();
    setDisplayFlex(container);
    container.appendChild(nowPlaying);
    return container;
  };
  const createInfos = (text) => {
    const container = createDiv();
    container.id = "jukebox-infos";
    container.style.placeItems = "center";
    container.style.placeContent = "center";
    setHeight6cqh(container);
    setDisplayFlex(container);
    const infosvg = createSvg(makeIconPath("info"));
    const infos2 = createP();
    infosvg.style.cursor = "pointer";
    const setOnClick = (cb) => infosvg.onclick = cb;
    infos2.textContent = text;
    infos2.style.fontSize = "0.8rem";
    infos2.style.marginLeft = "0.5rem";
    infos2.style.color = innerRed;
    const setInfosTextColor2 = setElementColor(infos2);
    container.appendChild(infosvg);
    container.appendChild(infos2);
    return [container, setOnClick, setInfosTextColor2];
  };
  const createSearchbar = (iconFilename) => {
    const container = createDiv();
    container.id = "search-bar";
    container.style.placeItems = "center";
    container.style.placeContent = "space-evenly";
    setHeight6cqh(container);
    const search = createP();
    setDisplayFlex(container);
    search.textContent = "SEARCH";
    search.style.fontSize = fontMid;
    const inputcontainer = createDiv();
    inputcontainer.id = "search-input";
    const setInputContainerStyle = () => {
      const style = inputcontainer.style;
      style.position = "relative";
    };
    setInputContainerStyle();
    const input = document.createElement("input");
    input.placeholder = "ARTIST TRACK + TITLE";
    inputcontainer.appendChild(input);
    const searchIcon = createSvg(makeIconPath(iconFilename));
    searchIcon.id = "search-icon";
    const inputValueCb2 = (onclick) => {
      searchIcon.onclick = (e) => {
        onclick(input.value);
      };
    };
    const setIconStyle = () => {
      const style = searchIcon.style;
      style.position = "absolute";
      style.top = "-5px";
      style.right = "0";
      style.height = "40px";
      style.cursor = "pointer";
    };
    const setIconSrc2 = (src) => searchIcon.setAttribute("src", iconpaths.get(src));
    setIconStyle();
    inputcontainer.appendChild(searchIcon);
    const setInputStyle = () => {
      const style = input.style;
      style.border = "none";
      style.padding = "0.5rem";
      style.width = "70cqw";
      setBorderRadius("15px")(input);
      style.backgroundColor = "white";
      style.setProperty("--c", innerRed);
    };
    setInputStyle();
    container.appendChild(search);
    container.appendChild(inputcontainer);
    const setPlaceHolderColor = (color) => input.style.setProperty("--c", placeholderColors.get(color));
    return [container, setPlaceHolderColor, inputValueCb2, setIconSrc2];
  };
  const createMain = () => {
    const container = createDiv();
    setHeight91cqh(container);
    container.id = "main";
    setDisplayGrid(container);
    container.style.gridTemplateRows = "1fr 1.4fr";
    return container;
  };
  const createSearchResult = (results2) => {
    const container = createDiv();
    container.id = "results-area";
    container.style.overflow = "scroll";
    container.style.backgroundColor = innerRed;
    setBorderRadius("4px")(container);
    setMargin1rem(container);
    const createResult = (result, i) => {
      const container2 = createDiv();
      container2.setAttribute("key", i.toString());
      container2.style.backgroundColor = "white";
      const text = createP();
      setElementStyle(container2, text);
      text.textContent = result;
      container2.appendChild(text);
      const setSearchResultCb = (cb) => {
        container2.onclick = cb;
      };
      return [container2, setSearchResultCb];
    };
    const setResultBackgroundColor2 = setElementBgColor(container);
    const showResult2 = (res) => {
      const resultsElements = res.map(createResult);
      resultsElements.forEach((e) => container.appendChild(e[0]));
      return resultsElements;
    };
    if (results2) {
      const resultsElements = showResult2(results2);
      return [
        container,
        resultsElements.map((e) => e[1]),
        setResultBackgroundColor2,
        showResult2
      ];
    } else {
      return [container, [], setResultBackgroundColor2, showResult2];
    }
  };
  const createQueue = (currentqueue2) => {
    const container = createDiv();
    container.style.overflow = "hidden";
    setDisplayGrid(container);
    container.id = "queue-area";
    const queueheader = createDiv();
    queueheader.id = "queue-header";
    setDisplayGrid(queueheader);
    queueheader.style.placeItems = "center";
    const createVote = () => {
      const vote2 = createP();
      vote2.textContent = "VOTE";
      vote2.style.color = innerRed;
      vote2.style.fontSize = "0.8rem";
      return vote2;
    };
    const voteL = createVote();
    const voteR = createVote();
    const setVoteColor2 = (color) => {
      [voteL, voteR].forEach((e) => setElementColor(e)(color));
    };
    const queue2 = createP();
    queue2.textContent = "QUEUE";
    if (vote) {
      queueheader.style.gridTemplateColumns = gridTemplate;
      queueheader.style.margin = "0 1rem";
      queueheader.appendChild(voteL);
      queueheader.appendChild(queue2);
      queueheader.appendChild(voteR);
    } else {
      queueheader.appendChild(queue2);
    }
    const content = createDiv();
    setMargin1rem(content);
    content.id = "content-area";
    content.style.overflow = "scroll";
    content.style.backgroundColor = innerRed;
    const setQueueBackgroundColor = setElementBgColor(content);
    setBorderRadius("4px")(content);
    const createQueueElement = (infostext, i) => {
      const element = createDiv();
      const infos2 = createP();
      infos2.style.backgroundColor = "white";
      infos2.style.width = "-webkit-fill-available";
      setBorderRadius("4px")(infos2);
      const margins = "2px";
      infos2.style.marginLeft = margins;
      infos2.style.marginRight = margins;
      element.id = "queue-el";
      element.setAttribute("key", i.toString());
      infos2.textContent = infostext;
      setElementStyle(element, infos2);
      if (vote) {
        setDisplayGrid(element);
        element.style.gridTemplateColumns = gridTemplate;
        const upButton = createButton(makeIconPath("upvote-arrow"));
        const setUpButtonCb = (cb) => {
          upButton.onclick = cb;
        };
        const downButton = createButton(makeIconPath("downvote-arrow"));
        const setDownButtonCb = (cb) => {
          downButton.onclick = cb;
        };
        element.appendChild(downButton);
        element.appendChild(infos2);
        element.appendChild(upButton);
        infos2.style.placeSelf = "center";
        return [element, setDownButtonCb, setUpButtonCb];
      } else {
        element.appendChild(infos2);
        return [element];
      }
    };
    const tracksnumber = createP();
    const tracks = 25;
    tracksnumber.textContent = `(${tracks} TRACKS QUEUED)`;
    tracksnumber.style.placeSelf = "center";
    tracksnumber.style.color = innerRed;
    tracksnumber.style.fontSize = "0.8rem";
    const setTrackInfoColor = (color) => setElementColor(tracksnumber)(color);
    content.style.marginTop = "0.2rem";
    content.style.marginBottom = "0.2rem";
    const queuelements = currentqueue2.map(createQueueElement);
    queuelements.forEach((e) => content.appendChild(e[0]));
    container.appendChild(queueheader);
    container.appendChild(content);
    container.appendChild(tracksnumber);
    return [
      container,
      [queuelements.map((e) => e[1]), queuelements.map((e) => e[2])],
      setQueueBackgroundColor,
      setVoteColor2,
      setTrackInfoColor
    ];
  };
  const createFooter = (icons) => {
    const container = createDiv();
    container.id = "footer-container";
    const subcontainer = createDiv();
    subcontainer.id = "footer-subcontainer";
    container.appendChild(subcontainer);
    setDisplayFlex(container);
    setDisplayGrid(subcontainer);
    const setContainerStyle = () => {
      const style = container.style;
      style.height = footerHeight + `cqh`;
      style.placeContent = "center";
      style.placeItems = "center";
    };
    const setSubContainerStyle = () => {
      const style = subcontainer.style;
      style.width = "90cqw";
      style.gridTemplateColumns = "1fr 1fr 1fr 1fr";
      style.placeSelf = "center";
      style.placeItems = "center";
    };
    setContainerStyle();
    setSubContainerStyle();
    const setColor = (color) => container.style.backgroundImage = gradientMap.get(color)[1];
    const elements = icons.map((e) => {
      const a = document.createElement("a");
      const svg = createSvg(e[0], "24px");
      a.appendChild(svg);
      a.style.cursor = "pointer";
      a.setAttribute("href", e[1]);
      a.setAttribute("target", "_blank");
      return a;
    });
    elements.forEach((e, i, a) => {
      subcontainer.appendChild(e);
    });
    return [container, setColor];
  };
  const webui = createWebui();
  const [tabs, tabEvents, setTabsHeights] = createTabs();
  const [display, setDisplayColor] = createDisplay();
  const header = createHeader("NIRVANA - SMELLS LIKE TEEN SPIRIT");
  const [infos, setInfosOnClick, setInfosTextColor] = createInfos("21 SATS PER TRACK & PER DOWN VOTE");
  const [searchbar, setPlaceholderColor, inputValueCb, setIconSrc] = createSearchbar("red-search");
  const main = createMain();
  const [
    searchResults,
    searchResultCbSetters,
    setResultBackgroundColor,
    showResult
  ] = createSearchResult(results);
  const [queue, votesCbs, setQueueBgColor, setVoteColor, setTracksInfoColor] = createQueue(currentqueue);
  const [footer, setFooterColor] = createFooter([
    [makeIconPath("telegram-icon"), "urltelegram"],
    [
      makeIconPath("github-cion"),
      "https://github.com/LightningJukeboxBot/LightningJukeboxBot"
    ],
    [makeIconPath("x-icon"), "urlx"],
    [makeIconPath("nrfm-icon"), "urlnrfm"]
  ]);
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
  const setActive = (tab) => {
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
  tabEvents.forEach((es) => es((e) => {
    const element = e.target;
    setActive(element.id);
  }));
  const searchResultCb = (cb) => {
    if (searchResultCbSetters.length !== 0)
      searchResultCbSetters.forEach((e) => e(cb));
  };
  const setVotesCb = (down, up) => {
    const downs = votesCbs[0];
    const ups = votesCbs[1];
    downs.forEach((d) => d(down));
    ups.forEach((d) => d(up));
  };
  setInfosOnClick(async (e) => {
    openPopUp("http://127.0.0.1:5500/web/popup.html", "_blank", "width=286, height=466");
  });
  return [webui, inputValueCb, searchResultCb, setVotesCb, showResult];
};
var body = document.body;
var results = getResults();
var queue = getQueue();
var [ui, inputValueCb, searchResultCb, setVotesCb, showResult] = createUI(queue);
body.prepend(ui);
inputValueCb((e) => {
  console.log(e);
  showResult(results);
});
searchResultCb((e) => console.log(e.currentTarget));
setVotesCb((e) => console.log(e.currentTarget), (e) => console.log(e.currentTarget));
