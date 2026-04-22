INSERT INTO tfm_bot.contacts (name, phone, email)
VALUES
('Ana García', '600111111', 'ana@email.com'),
('Carlos López', '600222222', 'carlos@email.com'),
('Marta Ruiz', '600333333', 'marta@email.com');


INSERT INTO tfm_bot.projects (
    contact_id,
    name,
    description,
    address,
    start_date
)
VALUES
(1, 'Reforma cocina', 'Reforma completa de cocina', 'Calle Mayor 12', '2026-03-01'),
(2, 'Reforma baño', 'Cambio de azulejos y ducha', 'Calle Sol 8', '2026-03-05'),
(3, 'Pintar salón', 'Pintura completa del salón', 'Avenida Norte 45', '2026-03-10');


INSERT INTO tfm_bot.tasks (
    project_id,
    contact_id,
    title,
    description,
    address,
    estimated_hours
)
VALUES
(1, 1, 'Demoler cocina', 'Retirada de muebles antiguos', 'Calle Mayor 12', 5),
(1, 1, 'Instalar fontanería', 'Instalación de tuberías nuevas', 'Calle Mayor 12', 6),
(1, 1, 'Instalar encimera', 'Encimera de mármol', 'Calle Mayor 12', 4),

(2, 2, 'Quitar azulejos', 'Retirada de azulejos antiguos', 'Calle Sol 8', 4),
(2, 2, 'Instalar ducha', 'Instalación de nueva ducha', 'Calle Sol 8', 5),
(3, 3, 'Pintar paredes', 'Pintura blanca mate', 'Avenida Norte 45', 6);
