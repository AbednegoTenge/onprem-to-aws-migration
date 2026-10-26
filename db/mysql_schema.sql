-- =====================================================================
-- ShopLite on-prem schema (MySQL 8.0)
--
-- Deliberately uses a few MySQL-isms so AWS SCT has something to convert:
--   * AUTO_INCREMENT             -> identity / serial
--   * ENUM                       -> varchar + CHECK (or enum type)
--   * TINYINT(1)                 -> smallint / boolean
--   * ON UPDATE CURRENT_TIMESTAMP-> trigger in PostgreSQL
--   * a VIEW and a STORED PROCEDURE
--
-- Every table has a PRIMARY KEY (DMS needs one for CDC updates/deletes).
-- No ON DELETE CASCADE: cascaded deletes are not written to the binlog
-- as separate row events, so DMS CDC would never see them.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS shopdb CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE shopdb;

CREATE TABLE IF NOT EXISTS customers (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    email       VARCHAR(255) NOT NULL,
    full_name   VARCHAR(150) NOT NULL,
    city        VARCHAR(100) NULL,
    is_active   TINYINT(1)   NOT NULL DEFAULT 1,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_customers_email (email),
    KEY ix_customers_city (city)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS products (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    sku         VARCHAR(40)    NOT NULL,
    name        VARCHAR(200)   NOT NULL,
    description TEXT           NULL,
    price       DECIMAL(10,2)  NOT NULL,
    stock       INT            NOT NULL DEFAULT 0,
    created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_products_sku (sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id INT UNSIGNED NOT NULL,
    status      ENUM('pending','paid','shipped','delivered','cancelled') NOT NULL DEFAULT 'pending',
    total       DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_orders_customer (customer_id),
    KEY ix_orders_status (status),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS order_items (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    order_id    INT UNSIGNED NOT NULL,
    product_id  INT UNSIGNED NOT NULL,
    quantity    INT           NOT NULL,
    unit_price  DECIMAL(10,2) NOT NULL,
    PRIMARY KEY (id),
    KEY ix_items_order (order_id),
    KEY ix_items_product (product_id),
    CONSTRAINT fk_items_order   FOREIGN KEY (order_id)   REFERENCES orders (id),
    CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE=InnoDB;

-- The write simulator inserts one row per second here. Comparing the newest
-- row on source vs target gives you the CDC replication lag.
CREATE TABLE IF NOT EXISTS heartbeat (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_host VARCHAR(100) NOT NULL,
    written_at  DATETIME(6)  NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW v_customer_revenue AS
SELECT c.id        AS customer_id,
       c.full_name AS full_name,
       c.city      AS city,
       COUNT(o.id) AS order_count,
       COALESCE(SUM(o.total), 0) AS revenue
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id AND o.status <> 'cancelled'
GROUP BY c.id, c.full_name, c.city;

DROP PROCEDURE IF EXISTS sp_customer_summary;
DELIMITER //
CREATE PROCEDURE sp_customer_summary(IN p_customer_id INT UNSIGNED)
BEGIN
    SELECT c.full_name,
           COUNT(o.id)               AS order_count,
           IFNULL(SUM(o.total), 0)   AS lifetime_value,
           MAX(o.created_at)         AS last_order_at
    FROM customers c
    LEFT JOIN orders o ON o.customer_id = c.id
    WHERE c.id = p_customer_id
    GROUP BY c.full_name;
END //
DELIMITER ;
