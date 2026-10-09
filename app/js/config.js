// Constants shared by every module: physical values, scene scale, colours and UI options.

export const EARTH_RADIUS_KM = 6378.137;   // equatorial radius, as used by SGP4
export const MU_KM3_S2 = 398600.4418;      // Earth's gravitational parameter
export const SCENE_UNITS_PER_KM = 1 / 1000; // 1 scene unit = 1,000 km

export const LEO_MAX_ALT_KM = 2000;        // upper edge of Low Earth Orbit

export const DATA_URL = 'data/leo_tle.json';
export const EARTH_TEXTURE_URL = 'https://cdn.jsdelivr.net/npm/three-globe@2.31.0/example/img/earth-blue-marble.jpg';

export const GROUP_COLORS = {
  Starlink: '#4ea8ff', OneWeb: '#ff9f43', Kuiper: '#b48cff', Qianfan: '#ff5c8a',
  Guowang: '#ffd84d', Iridium: '#3ddc97', Planet: '#9be15d', Spire: '#5ce1e6',
  Station: '#ffffff', Other: '#b7c0cf', Debris: '#ff4d4d',
};

// [time multiplier, button label]
export const SPEEDS = [[0, 'Pause'], [1, '1×'], [10, '10×'], [60, '60×'], [300, '300×'], [1000, '1000×'], [3600, '3600×'], [21600, '6 h/s']];

// Altitude breakpoints (km), spread evenly over the colour scale: most LEO traffic sits at 350-600 km.
export const ALTITUDE_STOPS_KM = [200, 300, 400, 500, 600, 800, 1000, 1400, 2000];
