// style
export const vote = true;
export const innerRed = `#ff8a8d`;
export const darkRed = `#c50200`;
export const superDark = `#720000`;
export const greenLight = `#a5f782`;
export const greencolor = `#73d04c`;
export const greenDark = `#27821a`;
export const blueLight = `#8ba5f6`;
export const bluecolor = `#5e84fc`;
export const blueDark = `#0d459d`;
export const fontMid = `"1.3rem"`;
export const tabwidth = 6;
export const tabHeight = 7;
export const footerHeight = 7;
export const gridTemplate = "1fr 5fr 1fr";
export const tabIconsSize = 32;
export const medSection = 6;
export const bigSection = 9;
export const spaces = [medSection, medSection, bigSection];

// links
export const telegramLink = "https://tnl.productions";
export const githubLink =
  "https://github.com/LightningJukeboxBot/LightningJukeboxBot";
export const xLink = "https://tnl.productions";
export const nrfmLink = "https://tnl.productions";

export type WebUiSettings = {
  container: {
    id: string;
    style: {
      height: string;
      containerType: string;
      fontFamily: string;
      color: string;
    };
  };
};
export const webUiSettings: WebUiSettings = {
  container: {
    id: "webui",
    style: {
      height: "100vh",
      containerType: "size",
      fontFamily: "boucherie-block",
      color: "white",
    },
  },
};

export type TabsSettings = {
  tabSize: string;
  height: (active: boolean) => "100%" | "60%";
  width: string;
  position: string;
  bottom: string;
  borderRadius: string;
  display: string;
  placeItems: string;
  transition: string;
  container: container;
  redTab: tab;
  greenTab: tab;
  blueTab: tab;
};
export const tabsSettings: TabsSettings = {
  tabSize: `${tabIconsSize}px`,
  height: (active: boolean) => (active ? "100%" : "60%"),
  width: tabwidth + `rem`,
  position: "absolute",
  bottom: "0",
  borderRadius: "5px 5px 0 0",
  display: "grid",
  placeItems: "center",
  transition: "height 0.5s ease",
  container: {
    id: "tab-area",
    position: "relative",
    height: tabHeight + "cqh",
  },
  redTab: {
    id: "red-tab",
    iconFilename: "Red-tab-icon",
    icon: { id: "red-tab-icon" },
    bgColor: darkRed,
  },
  greenTab: {
    id: "green-tab",
    iconFilename: "green-tab-icon",
    icon: { id: "green-tab-icon" },
    bgColor: greencolor,
    left: tabwidth + `rem`,
  },
  blueTab: {
    id: "blue-tab",
    iconFilename: "blue-tab-icon",
    icon: { id: "blue-tab-icon" },
    bgColor: bluecolor,
    left: tabwidth * 2 + `rem`,
  },
};
export type DisplaySettings = {
  container: {
    id: string;
    height: string;
    containerType: string;
  };
};
export const displaySettings: DisplaySettings = {
  container: {
    id: "display-area",
    height: 100 - [tabHeight, footerHeight].reduce((p, c) => p + c) + "cqh",
    containerType: "size",
  },
};

export type HeaderSettings = {
  container: {
    id: string;
    placeItems: string;
    placeContent: string;
  };
  nowPlaying: {
    margintb: string;
    marginlr: string;
    borders: string;
  };
  makeText: (track: string) => string;
};

export const headerSettings: HeaderSettings = {
  container: {
    id: "jb-header",
    placeItems: "center",
    placeContent: "space-evenly",
  },
  nowPlaying: {
    margintb: "0.3rem",
    marginlr: "0.2rem",
    borders: "solid 1px white",
  },
  makeText: (track: string) => `NOW PLAYING: ${track}`,
};
export type InfosSettings = {
  container: {
    id: string;
    placeItems: string;
    placeContent: string;
  };
  iconFilename: string;
  icon: {
    cursor: string;
  };
  p: {
    fontSize: string;
    marginLeft: string;
    color: string;
  };
};
export const infosSettings: InfosSettings = {
  container: {
    id: "jukebox-infos",
    placeItems: "center",
    placeContent: "center",
  },
  iconFilename: "info",
  icon: { cursor: "pointer" },
  p: { fontSize: "0.8rem", marginLeft: "0.5rem", color: innerRed },
};
export type container = {
  id: string;
  position: string;
  height: string;
};
export type tab = {
  id: string;
  iconFilename: string;
  icon: {
    id: string;
  };
  bgColor: string;
  left?: string;
};
export type inputContainerStyle = {
  position: string;
};
export type inputStyle = {
  placeHolder: string;
  style: {
    border: string;
    padding: string;
    width: string;
    borderRadius: string;
    bgColor: string;
    property: {
      key: string;
      value: string;
    };
    maxHeight: string;
  };
};
export type searchIcon = {
  id: string;
  style: {
    position: string;
    right: string;
    height: string;
    cursor: string;
    top: (top: string) => string;
  };
};

export type SearchSettings = {
  container: {
    id: string;
    style: {
      placeItems: string;
      placeContent: string;
    };
  };
  search: {
    textContent: string;
    style: {
      marginRight: string;
      fontSize: string;
    };
  };
  inputContainer: {
    id: string;
    style: inputContainerStyle;
  };
  input: inputStyle;
  searchIcon: searchIcon;
};

export const searchSettings = {
  container: {
    id: "search-bar",
    style: { placeItems: "center", placeContent: "center" },
  },
  search: {
    textContent: "SEARCH",
    style: { marginRight: "0.5rem", fontSize: fontMid },
  },
  inputContainer: { id: "search-input", style: { position: "relative" } },
  input: {
    placeHolder: "ARTIST TRACK + TITLE",
    style: {
      border: "none",
      padding: "0.5rem",
      width: "70cqw",
      borderRadius: "15px",
      bgColor: "white",
      property: { key: "--c", value: innerRed },
      maxHeight: "35px",
    },
  },
  searchIcon: {
    id: "searchIcon",
    style: {
      position: "absolute",
      right: "0",
      height: "40px",
      cursor: "pointer",
      top: (top: string) => `${top}px`,
    },
  },
};

export type MainSettings = {
  container: {
    id: string;
    style: {
      gridTemplateRows: string;
    };
  };
};
export const mainSettings: MainSettings = {
  container: { id: "main", style: { gridTemplateRows: "1fr 1.4fr" } },
};

export type SearchResultSettings = {
  container: {
    id: string;
    style: {
      overflow: string;
      bgColor: string;
      borderRadius: string;
    };
    attribute: {
      key: string;
    };
    result: {
      style: {
        backgroundColor: string;
      };
    };
  };
  results?: string[];
};
export const searchResultSettings = (
  results?: string[]
): SearchResultSettings => ({
  container: {
    id: "results-area",
    style: { overflow: "scroll", bgColor: innerRed, borderRadius: "4px" },
    attribute: { key: "key" },
    result: {
      style: { backgroundColor: "white" },
    },
  },
  results,
});

export type queueT = {
  textContent: string;
};
export type content = {
  id: string;
  style: {
    overflow: string;
    backgroundColor: string;
    borderRadius: string;
    marginTop: string;
    marginBottom: string;
  };
};
export type infosT = {
  style: {
    backgroundColor: string;
    width: string;
    borderRadius: string;
    magins: string;
    placeSelf: string;
  };
  element: {
    id: string;
    attribute: {
      key: string;
    };
  };
  upvoteFilename: string;
  downvoteFilename: string;
};
export type tracksNumber = {
  textContent: (tracks: string) => string;
  style: {
    placeSelf: string;
    color: string;
    fontSize: string;
  };
};
export type QueueSettings = {
  container: {
    style: {
      overflow: string;
    };
    id: string;
  };
  queueheader: {
    id: string;
    style: {
      placeItems: string;
      margin: string;
    };
  };
  vote: {
    textContent: string;
    style: {
      color: string;
      fontSize: string;
    };
  };
  queue: queueT;
  content: content;
  infos: infosT;
  tracksNumber: tracksNumber;
};
export const queueSettings: QueueSettings = {
  container: { style: { overflow: "hidden" }, id: "queue-area" },
  queueheader: {
    id: "queue-header",
    style: { placeItems: "center", margin: "0 1rem" },
  },
  vote: {
    textContent: "VOTE",
    style: { color: innerRed, fontSize: "0.8rem" },
  },
  queue: { textContent: "QUEUE" },
  content: {
    id: "content-area",
    style: {
      overflow: "scroll",
      backgroundColor: innerRed,
      borderRadius: "4px",
      marginTop: "0.2rem",
      marginBottom: "0.2rem",
    },
  },
  infos: {
    style: {
      backgroundColor: "white",
      width: "-webkit-fill-available",
      borderRadius: "4px",
      magins: "2px",
      placeSelf: "center",
    },
    element: {
      id: "queue-el",
      attribute: { key: "key" },
    },
    upvoteFilename: "upvote-arrow",
    downvoteFilename: "downvote-arrow",
  },
  tracksNumber: {
    textContent: (tracks: string) => `(${tracks} TRACKS QUEUED)`,
    style: { placeSelf: "center", color: innerRed, fontSize: "0.8rem" },
  },
};

export type icons = {
  style: {
    height: string;
    cursor: string;
    attribute1: {
      key: string;
    };
    attribute2: {
      key: string;
      value: string;
    };
    placeSelf: string;
    palceItems: string;
  };
};
export type FooterSettings = {
  container: {
    id: string;
    subcontainer: {
      id: string;
      style: {
        gridTemplateColumns: string;
        placeSelf: string;
        placeItems: string;
        width: string;
      };
    };
    style: {
      height: string;
      placeContent: string;
      placeItems: string;
    };
  };
  icons: icons;
};
export const footerSettings: FooterSettings = {
  container: {
    id: "footer-container",
    subcontainer: {
      id: "footer-subcontainer",
      style: {
        gridTemplateColumns: "1fr 1fr 1fr 1fr",
        placeSelf: "center",
        placeItems: "center",
        width: "90cqw",
      },
    },
    style: {
      height: footerHeight + `cqh`,
      placeContent: "center",
      placeItems: "center",
    },
  },
  icons: {
    style: {
      height: "24px",
      cursor: "pointer",
      attribute1: { key: "href" },
      attribute2: { key: "target", value: "_blank" },
      placeSelf: "center",
      palceItems: "center",
    },
  },
};
