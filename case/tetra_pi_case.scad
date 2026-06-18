// ─────────────────────────────────────────────────────────────────────────────
//  TetraMonitor — case voor Raspberry Pi 3B+ + RTL-SDR Blog V3
//
//  Open onderplaat: de Pi staat op 4 standoffs (M2.5), de dongle klikt in een
//  cradle ernaast met de SMA-connector vrij naar buiten. Korte USB-verleng van
//  de dongle naar een USB-poort van de Pi. Open ontwerp → alle poorten blijven
//  bereikbaar en je hoeft geen exacte poort-uitsparingen te raken.
//
//  Gebruik:  open in OpenSCAD →  F5 = voorbeeld,  F6 = render,  F7 = export STL.
//  Print:    PLA/PETG, 0.2 mm laag, 3 perimeters, 15–20% infill. Geen support.
//            Standoffs hebben voorboorgaten voor M2.5 zelftappende schroeven
//            (of boor 2.5 mm door en gebruik M2.5 + moer).
//
//  Alle maten in mm. Pas de variabelen hieronder aan naar smaak.
// ─────────────────────────────────────────────────────────────────────────────
$fn = 48;

/* [Tolerantie & wanden] */
tol      = 0.4;    // algemene speling voor passing
base_t   = 2.6;    // dikte onderplaat
margin   = 3.5;    // rand rondom alles
fillet   = 2;      // hoekafronding onderplaat

/* [Raspberry Pi 3B+] */
pi_l     = 85;     // bordlengte (X)
pi_w     = 56;     // bordbreedte (Y)
soh      = 6;      // standoff-hoogte (ruimte onder de Pi voor soldeerpunten)
post_d   = 6;      // diameter standoff
pilot_d  = 2.3;    // voorboorgat voor M2.5 zelftappend
// Pi-gatraster: rechthoek 58 x 49, 3.5 mm vanaf de hoek
pi_hx    = [3.5, 61.5];
pi_hy    = [3.5, 52.5];

/* [Dongle RTL-SDR Blog V3] */
dl       = 67;     // lengte metalen behuizing
dw       = 27;     // breedte
dh       = 13;     // hoogte
cradle_w = 2.4;    // wanddikte cradle
cradle_h = 9;      // hoogte van de klemwanden (~70% dongle, makkelijk in/uit)
gap      = 8;      // ruimte tussen Pi en dongle

/* [Opties] */
vents       = true;   // ventilatiegaten in de plaat
vent_d      = 5;      // diameter ventilatiegat
lid_posts   = true;   // hoekposten met gat voor een latere deksel/strap

// ── Afgeleide maten ──────────────────────────────────────────────────────────
base_l = margin + max(pi_l, dl) + margin;                 // X
base_w = margin + pi_w + gap + dw + margin;               // Y
pi_x0  = margin;                 pi_y0 = margin;           // Pi-hoek
dn_x0  = margin;                 dn_y0 = margin + pi_w + gap;  // dongle-hoek

// ── Bouwstenen ───────────────────────────────────────────────────────────────
module rrect(l, w, h, r) {                 // afgeronde plaat
    linear_extrude(h)
        offset(r) offset(-r)
            square([l, w]);
}

module standoff(x, y) {
    translate([x, y, base_t])
        difference() {
            cylinder(d = post_d, h = soh);
            translate([0, 0, -0.1])
                cylinder(d = pilot_d, h = soh + base_t + 0.2);
        }
}

module pi_standoffs() {
    for (hx = pi_hx, hy = pi_hy)
        standoff(pi_x0 + hx, pi_y0 + hy);
}

module dongle_cradle() {
    // vloer + twee klemwanden; één eindstop aan de USB-kant, open aan de SMA-kant
    translate([dn_x0, dn_y0, base_t]) {
        // klemwanden links/rechts
        for (yy = [-cradle_w, dw + tol])
            translate([0, yy, 0]) cube([dl, cradle_w, cradle_h]);
        // eindstop (USB-kant) met kabeluitsparing
        difference() {
            translate([dl, -cradle_w, 0]) cube([cradle_w, dw + tol + 2*cradle_w, cradle_h]);
            translate([dl - 0.1, dw/2 - 5, cradle_h - 4])
                cube([cradle_w + 0.2, 10, 5]);   // sleuf voor USB-stekker/kabel
        }
        // twee retentie-nokjes zodat de dongle netjes blijft zitten
        for (yy = [-cradle_w + 0.2, dw + tol + cradle_w - 1.0])
            translate([dl*0.4, yy, cradle_h - 0.1])
                rotate([0, 90, 0]) cylinder(d = 1.6, h = dl*0.2);
    }
}

module vent_grid(x0, y0, lx, ly) {
    nx = floor(lx / 10);
    ny = floor(ly / 10);
    for (i = [1:nx-1], j = [1:ny-1])
        translate([x0 + i*lx/nx, y0 + j*ly/ny, -0.1])
            cylinder(d = vent_d, h = base_t + 0.2);
}

module corner_posts() {
    for (cx = [margin/2, base_l - margin/2], cy = [margin/2, base_w - margin/2])
        translate([cx, cy, base_t])
            difference() {
                cylinder(d = 6, h = cradle_h);
                translate([0,0,-0.1]) cylinder(d = pilot_d, h = cradle_h + 0.2);
            }
}

// ── Samenstellen ─────────────────────────────────────────────────────────────
module tetra_case() {
    difference() {
        union() {
            rrect(base_l, base_w, base_t, fillet);   // onderplaat
            pi_standoffs();
            dongle_cradle();
            if (lid_posts) corner_posts();
        }
        if (vents) {
            vent_grid(pi_x0, pi_y0, pi_l, pi_w);                 // onder de Pi
            vent_grid(dn_x0, dn_y0, dl, dw);                     // onder de dongle
        }
    }
}

tetra_case();
